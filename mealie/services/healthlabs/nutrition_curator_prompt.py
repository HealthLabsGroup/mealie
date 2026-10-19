from __future__ import annotations

import json
import re
from pathlib import Path

from mealie.schema.healthlabs.nutrition_curator import NutritionCuratorCandidateSet

PERSONA_VERSION = "healthlabs-nutrition-curator-apple-v1"
PROMPT_VERSION = "healthlabs-nutrition-curator-v4"
POLICY_VERSION = "healthlabs-nutrition-curator-policy-v3"
_PROMPT_PATH = Path(__file__).resolve().parents[1] / "openai/prompts/healthlabs/nutrition-curator.txt"


class NutritionCuratorPrompt:
    @staticmethod
    def system() -> str:
        return _PROMPT_PATH.read_text().strip()

    @staticmethod
    def policy_only_control() -> str:
        prompt = NutritionCuratorPrompt.system()
        return re.sub(r"<identity>.*?</identity>\n?", "", prompt, count=1, flags=re.DOTALL)

    @staticmethod
    def input(candidate_set: NutritionCuratorCandidateSet) -> str:
        payload = json.dumps(candidate_set.prompt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
        return f"<untrusted_context>\n{payload}\n</untrusted_context>"
