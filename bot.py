"""Арена моделей: Мир vs RU (@ModelArenaRU_bot).

Один вопрос — три нейросети: «мировая» модель на выбор (через ProxyAPI) против GigaChat
и YandexGPT (прямое подключение к их API). Пользователь оценивает каждый ответ ⭐1–5,
бот копит личную и общую статистику. Бесплатно — 10 вопросов в день.

Сервер: systemd-служба model-arena-bot (см. deploy/), секреты — из *_FILE.
Локально: bash ../.claude/scripts/arena-bot-run.sh (ключи берёт скрипт).
"""
import asyncio
import html
import logging
import os
import re
import sys
import time
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

import providers
import storage
from catalog import DEFAULT_WORLD, FREE_DAILY_LIMIT, MODELS, RUSSIAN, TOPICS, WORLD, Model

# Журнал: bot.log до 1 МБ + 3 архива (bot.log.1…3), старое удаляется само — не больше ~4 МБ.
# В консоль — при ручном запуске и в Docker (LOG_TO_STDOUT=1 → docker logs).
_handlers: list[logging.Handler] = [RotatingFileHandler(
    storage.DATA_DIR / "bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
if sys.stdout.isatty() or os.environ.get("LOG_TO_STDOUT") == "1":
    _handlers.append(logging.StreamHandler(sys.stdout))


def _secret(name: str) -> str:
    """Значение из NAME или из файла NAME_FILE; пусто, если не задано."""
    if os.environ.get(name):
        return os.environ[name].strip()
    path = os.environ.get(name + "_FILE")
    return Path(path).read_text(encoding="utf-8").strip() if path and Path(path).exists() else ""


# Внешний журнал Better Stack (Logtail) — включается, только если заданы токен и адрес источника
_bs_token, _bs_host = _secret("BETTERSTACK_SOURCE_TOKEN"), _secret("BETTERSTACK_HOST")
BETTERSTACK_ON = bool(_bs_token and _bs_host)
if BETTERSTACK_ON:
    from logtail import LogtailHandler
    _handlers.append(LogtailHandler(source_token=_bs_token, host=_bs_host
                                    if _bs_host.startswith("http") else "https://" + _bs_host))
del _bs_token
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=_handlers)
# Служебный шум библиотек (каждый HTTP-запрос и каждое нажатие) — только при ошибках
for noisy in ("httpx", "httpx2", "aiogram.event"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("arena")
router = Router()

BTN_TOPIC = "🎲 Вопрос по теме"
BTN_OWN = "✍️ Свой вопрос"
BTN_MODEL = "⚙️ Модель"
BTN_STATS = "📊 Статистика"
MAX_QUESTION = 500
OWN_TOPIC = "own"  # так в базе помечены вопросы пользователя

# Админы (id через запятую) — без дневного лимита, для тестов
ADMINS = {int(x) for x in os.environ.get("ARENA_ADMINS", "").split(",") if x.strip().isdigit()}


class Own(StatesGroup):
    question = State()


# ---------- оформление ----------

def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text=BTN_TOPIC), KeyboardButton(text=BTN_OWN)],
        [KeyboardButton(text=BTN_MODEL), KeyboardButton(text=BTN_STATS)],
    ])


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def topics_kb() -> InlineKeyboardMarkup:
    items = list(TOPICS.items())
    return inline([[(title, f"t:{key}") for key, title in items[i:i + 2]]
                   for i in range(0, len(items), 2)])


def rating_kb(answer_id: int, current: int | None) -> InlineKeyboardMarkup:
    return inline([[(f"✅ {n}⭐" if n == current else f"{n}⭐", f"v:{answer_id}:{n}")
                    for n in range(1, 6)]])


def rub(x: float) -> str:
    return "<0,01 ₽" if 0 < x < 0.01 else f"{x:.2f} ₽".replace(".", ",")


def md_to_html(text: str) -> str:
    """Модели пишут в Markdown, Telegram понимает HTML — переводим самое частое."""
    t = html.escape(text.strip())
    t = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", t, flags=re.M)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"__(.+?)__", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"^\s*[-*]\s+", "• ", t, flags=re.M)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t[:3500]


def user_model(user_id: int) -> Model:
    m = MODELS.get(storage.get_model(user_id) or DEFAULT_WORLD)
    return m if m and not m.pro else MODELS[DEFAULT_WORLD]  # Pro-модели пока закрыты


def label(button: str) -> str:
    """Текст кнопки без эмодзи: «⚙️ Модель» → «Модель»."""
    return button.split(" ", 1)[1]


def topic_title(topic: str | None) -> str:
    return TOPICS.get(topic or "", "✍️ Свой вопрос")


# ---------- старт ----------

@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    storage.touch_user(m.from_user.id, m.from_user.full_name)
    world = user_model(m.from_user.id)
    await m.answer(
        "🏟 <b>Арена моделей: Мир vs RU</b>\n\n"
        f"Один вопрос — три нейросети:\n"
        f"🌍 <b>{world.title}</b> ({world.vendor}) — можно сменить в «{label(BTN_MODEL)}»\n"
        f"🇷🇺 <b>{RUSSIAN[0].title}</b> ({RUSSIAN[0].vendor})\n"
        f"🇷🇺 <b>{RUSSIAN[1].title}</b> ({RUSSIAN[1].vendor})\n\n"
        "Выбери тему — я придумаю вопрос, или задай свой. Модели ответят одновременно, "
        "а ты оценишь каждый ответ от 1 до 5 ⭐. Из оценок всех участников складывается рейтинг.\n\n"
        f"Ответы короткие — до ~1000 знаков. Бесплатно — {FREE_DAILY_LIMIT} вопросов в день.",
        reply_markup=main_kb())


# ---------- выбор темы и свой вопрос ----------

@router.message(F.text == BTN_TOPIC)
async def choose_topic(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Выбери тему — я придумаю вопрос:", reply_markup=topics_kb())


@router.callback_query(F.data == "topics")
async def topics_again(c: CallbackQuery):
    await c.answer()
    await c.message.answer("Выбери тему — я придумаю вопрос:", reply_markup=topics_kb())


def limit_left(user_id: int) -> int | None:
    """Сколько вопросов осталось сегодня (None — без лимита)."""
    if user_id in ADMINS:
        return None
    return max(0, FREE_DAILY_LIMIT - storage.rounds_today(user_id))


async def limit_reached(msg: Message, user_id: int) -> bool:
    if limit_left(user_id) == 0:
        await msg.answer(f"На сегодня все {FREE_DAILY_LIMIT} бесплатных вопросов использованы — "
                         "новые будут завтра.\n🔒 В Pro-версии вопросы без ограничений (скоро).")
        return True
    return False


@router.callback_query(F.data.startswith("t:"))
async def topic_chosen(c: CallbackQuery):
    topic = c.data[2:]
    await c.answer()
    storage.touch_user(c.from_user.id, c.from_user.full_name)
    if topic not in TOPICS or await limit_reached(c.message, c.from_user.id):
        return
    status = await c.message.answer(f"🎲 Придумываю вопрос на тему «{TOPICS[topic]}»…")
    try:
        question = await providers.random_question(TOPICS[topic], storage.recent_questions(topic))
    except Exception:  # noqa: BLE001
        log.exception("question generation failed")
        return await status.edit_text("Не получилось придумать вопрос — попробуй ещё раз "
                                      "или задай свой.", reply_markup=topics_kb())
    await status.edit_text(f"{TOPICS[topic]}\n❓ <b>{html.escape(question)}</b>")
    await run_round(c.message, c.from_user.id, topic, question)


@router.message(F.text == BTN_OWN)
@router.callback_query(F.data == "own")
async def own_question(event: Message | CallbackQuery, state: FSMContext):
    msg = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer()
    await state.set_state(Own.question)
    await msg.answer(f"Напиши свой вопрос (до {MAX_QUESTION} знаков) — его получат все три модели.")


@router.message(Own.question, F.text)
async def own_question_text(m: Message, state: FSMContext):
    if m.text in (BTN_TOPIC, BTN_MODEL, BTN_STATS):  # передумал — нажал другую кнопку меню
        await state.clear()
        return await {BTN_TOPIC: choose_topic, BTN_MODEL: choose_model, BTN_STATS: stats}[m.text](m, state)
    q = m.text.strip()
    if len(q) > MAX_QUESTION:
        return await m.answer(f"Слишком длинно ({len(q)} знаков) — сократи до {MAX_QUESTION}.")
    await state.clear()
    storage.touch_user(m.from_user.id, m.from_user.full_name)
    if await limit_reached(m, m.from_user.id):
        return
    await run_round(m, m.from_user.id, OWN_TOPIC, q)


# ---------- раунд: три модели отвечают одновременно ----------

class Progress:
    """Живое табло, пока модели отвечают (как в Нутрицио): раз в ~1,5 с обновляет сообщение
    (render(секунды) → текст) и держит в чате статус «печатает…»."""

    def __init__(self, msg: Message, render):
        self.msg, self.render = msg, render
        self.t0 = time.monotonic()
        self._task: asyncio.Task | None = None

    async def _loop(self):
        last = ""
        while True:
            try:
                await self.msg.bot.send_chat_action(self.msg.chat.id, "typing")
                text = self.render(int(time.monotonic() - self.t0))
                if text != last:
                    await self.msg.edit_text(text)
                    last = text
            except Exception:  # «message is not modified», сетевые мелочи — не мешаем раунду
                pass
            await asyncio.sleep(1.5)

    async def __aenter__(self):
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc):
        self._task.cancel()


EXPECTED_CHARS = 900  # примерная длина ответа — для полосы прогресса «пишет…»
BAR = 10


def round_board(models: list[Model], state: list[dict]):
    """Табло раунда: по строке на модель + общая полоса прогресса."""
    def render(sec: int) -> str:
        lines, total = [], 0.0
        for m, s in zip(models, state):
            if s["done"] is not None:
                part = 1.0
                status = (f"✅ готово за {s['done']:g} с" if not s["error"] else "⚠️ не ответила")
            elif s["chars"]:
                part = min(s["chars"] / EXPECTED_CHARS, 0.95)
                status = f"✍️ пишет… {s['chars']} зн."
            else:
                part = 0.0
                status = "🤔 думает…"
            total += part
            lines.append(f"{m.flag} <b>{m.title}</b> — {status}")
        filled = round(total / len(models) * BAR)
        done = sum(s["done"] is not None for s in state)
        return ("⏳ <b>Модели отвечают…</b>\n\n" + "\n".join(lines) +
                f"\n\n{'▰' * filled}{'▱' * (BAR - filled)}  {done} из {len(models)}\n⏱ {sec} с")
    return render


_busy: set[int] = set()  # у кого раунд уже идёт — второй параллельно не запускаем


async def run_round(msg: Message, user_id: int, topic: str, question: str) -> None:
    if user_id in _busy:
        await msg.answer("⏳ Модели ещё отвечают на прошлый вопрос — дождись ответов.")
        return
    _busy.add(user_id)
    try:
        await _run_round(msg, user_id, topic, question)
    finally:
        _busy.discard(user_id)


async def _run_round(msg: Message, user_id: int, topic: str, question: str) -> None:
    models = [user_model(user_id), *RUSSIAN]
    round_id = storage.new_round(user_id, topic, question)
    log.info("round %s user=%s topic=%s models=%s", round_id, user_id, topic, [m.key for m in models],
             extra={"event": "round", "round_id": round_id, "user_id": user_id, "topic": topic})
    state = [{"chars": 0, "done": None, "error": False} for _ in models]
    render = round_board(models, state)
    board = await msg.answer(render(0))

    async def one(slot: int, model: Model):
        s = state[slot]
        a = await providers.ask(model, question, on_text=lambda t: s.update(chars=len(t)))
        s.update(done=a.seconds, error=bool(a.error))
        return a

    # Ждём всех — на табло видно, кто и за сколько ответил; ответы потом идут в одном порядке
    async with Progress(board, render):
        answers = await asyncio.gather(*(one(i, m) for i, m in enumerate(models)))
    await board.delete()

    for slot, (model, a) in enumerate(zip(models, answers)):
        answer_id = storage.add_answer(round_id, slot, model.key, a)
        head = f"{model.flag} <b>{model.title}</b> · {model.vendor}\n"
        if a.error:  # подробности — в логе, пользователю — понятными словами
            await msg.answer(head + "⚠️ Модель сейчас не ответила — в итоге раунда она пропускается.")
            continue
        await msg.answer(head + f"⏱ {a.seconds:g} с · 💰 {rub(a.cost)}\n\n{md_to_html(a.text)}",
                         reply_markup=rating_kb(answer_id, None))

    left = limit_left(user_id)
    await msg.answer("👆 Оцени каждый ответ ⭐ — после этого покажу итог раунда."
                     + (f"\nОсталось вопросов на сегодня: {left}" if left is not None else ""))
    await maybe_summary(msg, round_id)  # если все модели упали — итог сразу


@router.callback_query(F.data.startswith("v:"))
async def vote(c: CallbackQuery):
    _, answer_id, score = c.data.split(":")
    round_id = storage.rate(int(answer_id), c.from_user.id, int(score))
    if round_id is None:
        return await c.answer("Оценивать может только тот, кто задал вопрос")
    log.info("vote user=%s answer=%s score=%s", c.from_user.id, answer_id, score,
             extra={"event": "vote", "user_id": c.from_user.id, "score": score})
    await c.message.edit_reply_markup(reply_markup=rating_kb(int(answer_id), int(score)))
    await c.answer(f"Оценка {score}⭐ сохранена")
    await maybe_summary(c.message, round_id)


async def maybe_summary(msg: Message, round_id: int) -> None:
    """Когда оценены все ответы раунда — итог и кнопки «что дальше»."""
    answers = storage.round_answers(round_id)
    if any(a["rating"] is None and not a["error"] for a in answers):
        return
    if not storage.mark_summarized(round_id):
        return
    rnd = storage.get_round(round_id)
    lines = []
    for a in sorted(answers, key=lambda a: (a["rating"] or 0), reverse=True):
        mdl = MODELS[a["model_key"]]
        if a["error"]:
            lines.append(f"{mdl.flag} {mdl.title} — не ответила")
        else:
            lines.append(f"{mdl.flag} <b>{mdl.title}</b> — {a['rating']}⭐ · ⏱ {a['seconds']:g} с · 💰 {rub(a['cost'])}")
    again = (("🎲 Ещё вопрос на эту тему", f"t:{rnd['topic']}") if rnd["topic"] in TOPICS
             else ("✍️ Ещё свой вопрос", "own"))
    await msg.answer("🏁 <b>Итог раунда</b>\n\n" + "\n".join(lines),
                     reply_markup=inline([[again], [("📚 Другая тема", "topics"), ("📊 Рейтинг", "st:all")]]))


# ---------- выбор «мировой» модели ----------

def models_kb(current: str) -> InlineKeyboardMarkup:
    rows = []
    for m in WORLD:
        if m.pro:
            rows.append([(f"🔒 {m.title} · {m.vendor} — Pro", f"m:{m.key}")])
        else:
            mark = "✅ " if m.key == current else ""
            rows.append([(f"{mark}{m.title} · {m.vendor}", f"m:{m.key}")])
    return inline(rows)


@router.message(F.text == BTN_MODEL)
async def choose_model(m: Message, state: FSMContext | None = None):
    if state:
        await state.clear()
    storage.touch_user(m.from_user.id, m.from_user.full_name)
    cur = user_model(m.from_user.id)
    await m.answer(
        "⚙️ <b>Модель от «мира»</b> — с ней соревнуются GigaChat и YandexGPT.\n"
        f"Сейчас: <b>{cur.title}</b>. Модели с 🔒 — флагманы, будут в Pro-версии.",
        reply_markup=models_kb(cur.key))


@router.callback_query(F.data.startswith("m:"))
async def model_chosen(c: CallbackQuery):
    m = MODELS.get(c.data[2:])
    if not m:
        return await c.answer()
    if m.pro:
        return await c.answer(f"🔒 {m.title} будет доступна в Pro-версии", show_alert=True)
    storage.touch_user(c.from_user.id, c.from_user.full_name)
    storage.set_model(c.from_user.id, m.key)
    await c.message.edit_text(
        f"⚙️ Готово: теперь с российскими моделями соревнуется <b>{m.title}</b> ({m.vendor}).",
        reply_markup=models_kb(m.key))
    await c.answer("Модель выбрана")


# ---------- статистика ----------

def stats_kb() -> InlineKeyboardMarkup:
    return inline([[("🌐 Общая", "st:all"), ("👤 Моя", "st:me"), ("🗂 По темам", "st:topics")]])


def stats_text(kind: str, user_id: int) -> str:
    if kind == "topics":
        best: dict[str, tuple] = {}
        for r in storage.topic_stats():
            cur = best.get(r["topic"])
            if not cur or (r["avg_rating"], r["n"]) > (cur[1], cur[2]):
                best[r["topic"]] = (r["model_key"], r["avg_rating"], r["n"])
        if not best:
            return "🗂 <b>Лучшие модели по темам</b>\n\nПока нет оценок."
        lines = [f"{topic_title(t)} — <b>{MODELS[k].title}</b> {avg:.1f}⭐ ({n})"
                 for t, (k, avg, n) in sorted(best.items(), key=lambda x: topic_title(x[0]))]
        return "🗂 <b>Лучшие модели по темам</b> (средняя оценка, число оценок)\n\n" + "\n".join(lines)

    mine = kind == "me"
    rows = storage.model_stats(user_id if mine else None)
    tot = storage.totals(user_id if mine else None)
    title = "👤 <b>Мой рейтинг моделей</b>" if mine else "🌐 <b>Общий рейтинг моделей</b>"
    if not rows:
        return f"{title}\n\nПока нет оценок — задай вопрос и оцени ответы."
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i] if i < 3 else f'{i + 1}.'} {MODELS[r['model_key']].flag} "
             f"<b>{MODELS[r['model_key']].title}</b> — {r['avg_rating']:.2f}⭐ ({r['n']})\n"
             f"      ⏱ {r['avg_seconds']:.1f} с · 💰 {rub(r['avg_cost'])} за ответ"
             for i, r in enumerate(rows)]
    foot = (f"Раундов: {tot['rounds']}" if mine
            else f"Раундов: {tot['rounds']} · участников: {tot['users']}")
    return f"{title}\n\n" + "\n".join(lines) + f"\n\n{foot}"


@router.message(F.text == BTN_STATS)
async def stats(m: Message, state: FSMContext | None = None):
    if state:
        await state.clear()
    await m.answer(stats_text("all", m.from_user.id), reply_markup=stats_kb())


@router.callback_query(F.data.startswith("st:"))
async def stats_switch(c: CallbackQuery):
    kind = c.data[3:]
    text = stats_text(kind, c.from_user.id)
    await c.answer()
    kb = c.message.reply_markup
    on_stats = kb and [b.callback_data for b in kb.inline_keyboard[0]] == ["st:all", "st:me", "st:topics"]
    if on_stats:  # уже экран статистики — переключаем на месте, а не новым сообщением
        if c.message.html_text != text:
            await c.message.edit_text(text, reply_markup=stats_kb())
    else:
        await c.message.answer(text, reply_markup=stats_kb())


@router.message()
async def fallback(m: Message):
    await m.answer("Выбери действие на кнопках ниже 👇", reply_markup=main_kb())


async def main():
    token = os.environ.get("BOT_TOKEN")
    if not token and os.environ.get("BOT_TOKEN_FILE"):
        token = Path(os.environ["BOT_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("Нет токена — задайте BOT_TOKEN или BOT_TOKEN_FILE")
    storage.init()
    # Локально Telegram доступен через системный прокси Windows (VPN-клиент)
    proxy = urllib.request.getproxies().get("https")
    session = AiohttpSession(proxy=proxy) if proxy else None
    log.info("proxy: %s", proxy or "нет")
    log.info("внешний журнал Better Stack: %s", "включён" if BETTERSTACK_ON else "выключен")
    bot = Bot(token, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
