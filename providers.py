"""Подключение моделей: ProxyAPI (библиотека openai), GigaChat и YandexGPT (их собственные API).

Для бота все три выглядят одинаково: ask(модель, вопрос) → Answer (текст, время, токены, цена).

Ключи — из переменных окружения или файлов (*_FILE на сервере), в коде их нет:
  PROXYAPI_KEY, GIGACHAT_AUTH_KEY, YANDEX_API_KEY, YANDEX_FOLDER_ID.
"""
import asyncio
import json
import logging
import os
import random
import ssl
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import aiohttp
from openai import AsyncOpenAI

from catalog import QUESTION_MODEL, Model

log = logging.getLogger(__name__)

SYSTEM = ("Ты участвуешь в сравнении нейросетей: пользователь задаёт один и тот же вопрос "
          "нескольким моделям и оценивает ответы. Отвечай на русском языке, точно и по существу, "
          "не длиннее 900 знаков. Без таблиц; можно короткие списки и **жирный** шрифт.")
MAX_TOKENS = 700
TIMEOUT = 90  # секунд на один ответ

PROXYAPI_URL = "https://openai.api.proxyapi.ru/v1"
GIGA_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGA_CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
GIGA_SCOPE = "GIGACHAT_API_PERS"  # физлицо
YANDEX_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

# Серверы Сбера подписаны сертификатом Минцифры — его нет в стандартных хранилищах
GIGA_CA = Path(__file__).with_name("certs") / "russian_trusted_root_ca.pem"


def secret(name: str) -> str:
    """Секрет из переменной NAME или из файла, путь к которому в NAME_FILE."""
    value = os.environ.get(name)
    if not value and os.environ.get(name + "_FILE"):
        value = Path(os.environ[name + "_FILE"]).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"Нет секрета {name} — задайте {name} или {name}_FILE")
    return value


OnText = Callable[[str], None] | None  # получает накопленный текст ответа по мере написания


@dataclass
class Answer:
    text: str
    seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0          # ₽ по цене каталога
    error: str | None = None


# ---------- ProxyAPI ----------

_openai: AsyncOpenAI | None = None


def _proxyapi() -> AsyncOpenAI:
    global _openai
    if _openai is None:
        _openai = AsyncOpenAI(api_key=secret("PROXYAPI_KEY"), base_url=PROXYAPI_URL,
                              timeout=TIMEOUT, max_retries=1)
    return _openai


async def _ask_proxyapi(model: Model, system: str, question: str,
                        on_text: OnText = None) -> tuple[str, int, int]:
    """Ответ приходит по кусочкам (streaming) — бот видит, сколько модель уже написала."""
    if "gpt-5" in model.api_name:  # рассуждающие модели: свой лимит и без temperature
        extra = {"max_completion_tokens": 4000, "reasoning_effort": "low"}
    else:
        extra = {"max_tokens": MAX_TOKENS, "temperature": 0.7}
    stream = await _proxyapi().chat.completions.create(
        model=model.api_name,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": question}],
        stream=True, stream_options={"include_usage": True}, **extra)
    text, usage = "", None
    async for chunk in stream:
        usage = chunk.usage or usage
        if chunk.choices and chunk.choices[0].delta.content:
            text += chunk.choices[0].delta.content
            if on_text:
                on_text(text)
    return text, usage.prompt_tokens if usage else 0, usage.completion_tokens if usage else 0


# ---------- GigaChat: ключ авторизации → токен доступа на 30 минут ----------

_giga_token: str | None = None
_giga_expires = 0.0                 # unix-время в секундах
_giga_lock = asyncio.Lock()         # один обмен токена за раз
_giga_slot = asyncio.Semaphore(1)   # Freemium-пакет: генерация в одном потоке
_giga_ssl: ssl.SSLContext | None = None


def _giga_ssl_ctx() -> ssl.SSLContext:
    global _giga_ssl
    if _giga_ssl is None:
        _giga_ssl = ssl.create_default_context()
        _giga_ssl.load_verify_locations(GIGA_CA)
    return _giga_ssl


async def _giga_access_token(session: aiohttp.ClientSession) -> str:
    """Токен живёт 30 минут — берём новый, когда до конца осталось меньше минуты."""
    global _giga_token, _giga_expires
    async with _giga_lock:
        if _giga_token and time.time() < _giga_expires - 60:
            return _giga_token
        async with session.post(
                GIGA_OAUTH_URL, data={"scope": GIGA_SCOPE}, ssl=_giga_ssl_ctx(),
                headers={"Authorization": "Basic " + secret("GIGACHAT_AUTH_KEY"),
                         "RqUID": str(uuid.uuid4()), "Accept": "application/json"}) as r:
            body = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"GigaChat OAuth {r.status}: {body.get('message', body)}")
        _giga_token = body["access_token"]
        _giga_expires = body["expires_at"] / 1000  # приходит в миллисекундах
        log.info("GigaChat: новый токен доступа до %s", time.strftime("%H:%M", time.localtime(_giga_expires)))
        return _giga_token


async def _ask_gigachat(model: Model, system: str, question: str,
                        on_text: OnText = None) -> tuple[str, int, int]:
    async with aiohttp.ClientSession() as s, _giga_slot:
        token = await _giga_access_token(s)
        async with s.post(GIGA_CHAT_URL, ssl=_giga_ssl_ctx(),
                          headers={"Authorization": f"Bearer {token}"},
                          json={"model": model.api_name, "max_tokens": MAX_TOKENS, "temperature": 0.7,
                                "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": question}]}) as r:
            body = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"GigaChat {r.status}: {body.get('message', body)}")
    u = body.get("usage", {})
    return body["choices"][0]["message"]["content"], u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


# ---------- YandexGPT (Yandex Cloud, API-ключ сервисного аккаунта) ----------

async def _ask_yandex(model: Model, system: str, question: str,
                      on_text: OnText = None) -> tuple[str, int, int]:
    folder = secret("YANDEX_FOLDER_ID")
    async with aiohttp.ClientSession() as s:
        async with s.post(YANDEX_URL,
                          headers={"Authorization": "Api-Key " + secret("YANDEX_API_KEY"),
                                   "x-folder-id": folder},
                          json={"modelUri": f"gpt://{folder}/{model.api_name}",
                                "completionOptions": {"stream": False, "temperature": 0.7,
                                                      "maxTokens": str(MAX_TOKENS)},
                                "messages": [{"role": "system", "text": system},
                                             {"role": "user", "text": question}]}) as r:
            body = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"YandexGPT {r.status}: {body.get('message') or body.get('error', body)}")
    res = body["result"]
    u = res.get("usage", {})
    return (res["alternatives"][0]["message"]["text"],
            int(u.get("inputTextTokens", 0)), int(u.get("completionTokens", 0)))


_CALL = {"proxyapi": _ask_proxyapi, "gigachat": _ask_gigachat, "yandex": _ask_yandex}


async def ask(model: Model, question: str, system: str = SYSTEM, on_text: OnText = None) -> Answer:
    """Один ответ модели. Ошибки не роняют раунд — возвращаются в Answer.error.
    on_text — ход написания (сейчас только у ProxyAPI; GigaChat и Yandex отвечают целиком)."""
    t0 = time.monotonic()
    try:
        text, tin, tout = await asyncio.wait_for(
            _CALL[model.provider](model, system, question, on_text), TIMEOUT)
    except Exception as e:  # noqa: BLE001 — любая ошибка провайдера = «модель не ответила»
        secs = round(time.monotonic() - t0, 1)
        msg = "не ответила за отведённое время" if isinstance(e, asyncio.TimeoutError) else str(e)[:200]
        log.warning("AI %s failed in %ss: %r", model.key, secs, e)
        return Answer("", secs, error=msg)
    secs = round(time.monotonic() - t0, 1)
    cost = (tin * model.price_in + tout * model.price_out) / 1_000_000
    log.info("AI %s: %ss, tokens %s/%s, %.3f ₽", model.key, secs, tin, tout, cost)
    return Answer(text.strip(), secs, tin, tout, cost)


QUESTION_KINDS = [
    "«почему так происходит?» — объяснение причины",
    "«как это устроено / как работает?»",
    "сравнение двух известных понятий, явлений или событий",
    "практический вопрос «как правильно сделать…?»",
    "вопрос о известном факте с просьбой объяснить его значение",
    "«объясни простыми словами» — сложное понятие для новичка",
    "вопрос-развенчание распространённого мифа или заблуждения",
]


async def random_question(topic_title: str, avoid: list[str]) -> str:
    """Придумывает случайный вопрос по теме (дешёвая модель, ответ в JSON).
    Модель склонна выдумывать «факты» для эффектности — промпт это прямо запрещает."""
    avoid_text = "\n".join(f"- {q}" for q in avoid[:15]) or "—"
    system = ("Ты придумываешь вопросы для сравнения нейросетей. Вопрос — короткий (до 200 знаков), "
              "конкретный, интересный широкому кругу, ответ на него укладывается в 900 знаков. "
              "Вопрос опирается ТОЛЬКО на достоверные, общеизвестные факты: не выдумывай события, "
              "реформы, людей, даты и термины; если не уверен в факте — выбери другой. "
              'Ответ — строго JSON: {"question": "..."}')
    user = (f"Тема: {topic_title}. Тип вопроса: {random.choice(QUESTION_KINDS)}. "
            f"Придумай один вопрос. Не повторяй уже заданные:\n{avoid_text}")
    r = await _proxyapi().chat.completions.create(
        model=QUESTION_MODEL.api_name, temperature=1.0, max_tokens=200,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    return json.loads(r.choices[0].message.content)["question"].strip()
