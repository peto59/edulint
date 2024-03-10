from typing import Any, Dict, ClassVar, List
from dataclasses import dataclass
import inspect
import pkgutil
import importlib
import re

from loguru import logger

import pylint
from pylint.checkers import BaseChecker

from edulint.option_parses import LANG_TRANSLATION_WORDS_LABEL
from edulint.config.raw_flake8_patterns import FLAKE8
from edulint.linting import checkers as edulint_checkers


def to_pattern(message: str):
    return re.sub("%[rsd]", "(.*)", re.escape(message), flags=re.IGNORECASE)


def checker_classes(module):
    for module_info in pkgutil.walk_packages(module.__path__):
        submodule = importlib.import_module(f"{module.__name__}.{module_info.name}")
        for name, obj in inspect.getmembers(submodule):
            if inspect.isclass(obj) and issubclass(obj, BaseChecker) and obj != BaseChecker:
                yield obj


def get_edulint_patterns():
    messages = {}
    for checker in checker_classes(edulint_checkers):
        messages.update({code: info[0] for code, info in checker.msgs.items()})

    return messages


def get_pylint_patterns():
    messages = {}
    for checker in checker_classes(pylint.checkers):
        messages.update({code: info[0] for code, info in checker.msgs.items()})

    return messages


def get_patterns():
    return {
        code: to_pattern(message)
        for code, message in {**get_pylint_patterns(), **get_edulint_patterns(), **FLAKE8}.items()
    }


@dataclass
class Translation:
    translation: str
    words: Dict[str, Dict[str, str]]
    patterns: ClassVar[Dict[str, str]] = get_patterns()

    def translate_words(self, words: List[str]) -> List[str]:
        result = []
        for i, word in enumerate(words):
            mapping = self.words.get(str(i + 1))
            if mapping is not None:
                result.append(mapping.get(word, word))
            else:
                result.append(word)
        return result

    def translate(self, code: str, message: str):
        pattern = self.patterns.get(code)
        if pattern is None or "(.*)" not in pattern:
            return self.translation

        match = re.match(pattern, message, flags=re.IGNORECASE)
        if not match:
            return self.translation
        return self.translation.format(*self.translate_words(match.groups()))


LangTranslations = Dict[str, Translation]


def parse_lang_translations(raw_lang_translations: Any) -> LangTranslations:
    if not isinstance(raw_lang_translations, dict):
        logger.warning(
            "langauge translations are not a dictionary but a value {val} of type {type}",
            val=raw_lang_translations,
            type=type(raw_lang_translations),
        )
        return None

    translation_words = raw_lang_translations.get(LANG_TRANSLATION_WORDS_LABEL, {})
    if not isinstance(translation_words, dict):
        logger.warning(
            "translation for specific words is not a dictionary but a value of type {type}",
            type=type(translation_words),
        )
        translation_words = {}
    for id_, val in translation_words.items():
        if not isinstance(val, dict):
            logger.warning(
                "translation words for identifier {id_} is not a dictionary but a value of type {type}",
                id_=id_,
                type=type(val),
            )
            continue
        for order, mapping in val.items():
            if not order.isdecimal():
                logger.warning(
                    "order value {order} of translation words for identifier {id_} does not contain integer",
                    order=order,
                    id_=id_,
                )
            if not isinstance(mapping, dict):
                logger.warning(
                    "translation words mapping for order {order} of identifier {id_} is not a dicitonary but a value of type {type}",
                    order=order,
                    id_=id_,
                    type=type(mapping),
                )
                continue
            for words, translated in mapping.items():
                if not isinstance(translated, str):
                    logger.warning(
                        "translation for words {words} for order {order} of identifier {id_} is not a string but a value of type {type}",
                        words=words,
                        order=order,
                        id_=id_,
                        type=type(translated),
                    )

    lang_translations = {}
    for id_, translation in raw_lang_translations.items():
        if id_ == LANG_TRANSLATION_WORDS_LABEL:
            continue
        if not isinstance(translation, str):
            logger.warning(
                "translation for identifier {id_} is not a string but a value of type {type}",
                id_=id_,
                type=type(translation),
            )
            continue

        lang_translations[id_] = Translation(translation, translation_words.get(id_, {}))

    return lang_translations
