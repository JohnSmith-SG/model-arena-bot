"""Каталог: модели-участники арены и темы вопросов.

Цены — ₽ за 1 млн токенов (ввод / вывод), сверены 2026-10-16:
ProxyAPI — proxyapi.ru/pricing (gpt-4o-mini — по счёту прошлого урока),
GigaChat — платный пакет для физлиц (в бесплатном Freemium-пакете ответы не списывают деньги,
но цену показываем «как если бы платили» — чтобы сравнение было честным),
YandexGPT — aistudio.yandex.ru/docs/ru/ai-studio/pricing.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Model:
    key: str            # внутренний id (в базе и в кнопках)
    title: str          # как называем в боте
    vendor: str         # разработчик
    provider: str       # через кого подключаемся: proxyapi | gigachat | yandex
    api_name: str       # имя модели в API провайдера
    price_in: float     # ₽ за 1 млн токенов запроса
    price_out: float    # ₽ за 1 млн токенов ответа
    pro: bool = False   # 🔒 только в платной версии
    flag: str = "🌍"


# Первое место в раунде — «мировая» модель на выбор пользователя (через ProxyAPI)
WORLD = [
    Model("gpt-4o-mini", "GPT-4o mini", "OpenAI", "proxyapi", "openai/gpt-4o-mini", 39, 155),
    Model("deepseek-v4-flash", "DeepSeek V4 Flash", "DeepSeek", "proxyapi",
          "deepseek/deepseek-v4-flash", 20, 40),
    Model("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite", "Google", "proxyapi",
          "google/gemini-3.1-flash-lite", 76, 455),
    # Флагманы — пока под замком (платная версия)
    Model("gpt-5.5", "GPT-5.5", "OpenAI", "proxyapi", "openai/gpt-5.5", 1520, 9100, pro=True),
    Model("claude-sonnet-5-5", "Claude Sonnet 5.5", "Anthropic", "proxyapi",
          "anthropic/claude-sonnet-5-5", 500, 2500, pro=True),
    Model("gemini-3.1-pro", "Gemini 3.1 Pro", "Google", "proxyapi",
          "google/gemini-3.1-pro-preview", 600, 3640, pro=True),
]

# Второе и третье места — всегда российские модели (прямое подключение, тема урока VCb02)
GIGACHAT = Model("gigachat", "GigaChat 2 Pro", "Сбер", "gigachat", "GigaChat-2-Pro", 500, 500, flag="🇷🇺")
YANDEXGPT = Model("yandexgpt", "YandexGPT Pro 5.1", "Яндекс", "yandex", "yandexgpt/rc", 800, 800, flag="🇷🇺")
RUSSIAN = [GIGACHAT, YANDEXGPT]

DEFAULT_WORLD = "gpt-4o-mini"
QUESTION_MODEL = WORLD[0]   # придумывает случайные вопросы по теме — дёшево и быстро

MODELS = {m.key: m for m in WORLD + RUSSIAN}

TOPICS = {
    "history": "🏛 История",
    "popsci": "🔭 Популярная наука",
    "tech": "💻 Технологии и программирование",
    "finance": "💰 Финансы и экономика",
    "cooking": "🍳 Кулинария",
    "travel": "🧭 Путешествия и география",
    "law": "⚖️ Право",
    "math": "🧮 Математика и логика",
    "russian": "📖 Русский язык",
    "art": "🎨 Искусство и культура",
}

FREE_DAILY_LIMIT = 10   # вопросов в день в бесплатной версии; в Pro — без ограничений (позже)
