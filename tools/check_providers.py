"""Проверка подключений: задаёт один вопрос выбранным моделям и печатает ответ, время и цену.

    python tools/check_providers.py                 — GPT-4o mini, GigaChat, YandexGPT
    python tools/check_providers.py gigachat        — только указанные ключи моделей
    python tools/check_providers.py --question      — ещё и сгенерировать вопрос по теме
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import providers  # noqa: E402
from catalog import MODELS, TOPICS  # noqa: E402

QUESTION = "Почему небо голубое? Объясни коротко."


async def main(args: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if "--question" in args:
        args.remove("--question")
        q = await providers.random_question(TOPICS["history"], [])
        print("Сгенерированный вопрос:", q)
    keys = args or ["gpt-4o-mini", "gigachat", "yandexgpt"]
    answers = await asyncio.gather(*(providers.ask(MODELS[k], QUESTION) for k in keys))
    for k, a in zip(keys, answers):
        print(f"\n=== {MODELS[k].title}: {a.seconds} с, токены {a.input_tokens}/{a.output_tokens}, "
              f"{a.cost:.3f} ₽, {len(a.text)} знаков")
        print(a.error and f"ОШИБКА: {a.error}" or a.text)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
