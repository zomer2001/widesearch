"""Extraction runtime policy, independent of the research/chat model settings.

Model calls stay off while the database interfaces are being assembled. Offline
imports and explicitly injected test extractors remain available. Enabling this
flag is a separate, deliberate deployment change after model access is verified.
"""

MODEL_EXTRACTION_ENABLED = False
EXTRACTION_ENGINE = "cli"
EXTRACTION_MODEL = "gpt-6-luna"
EXTRACTION_REASONING = "low"
PAUSED_MESSAGE = "模型抽取已暂停；可查看原文、导入已有结果或提交结构化字段结果。"


def runtime():
    return {
        "model_enabled": MODEL_EXTRACTION_ENABLED,
        "engine": EXTRACTION_ENGINE,
        "model": EXTRACTION_MODEL,
        "reasoning_effort": EXTRACTION_REASONING,
        "message": "模型抽取已启用" if MODEL_EXTRACTION_ENABLED else PAUSED_MESSAGE,
    }
