"""
QwenAIService — centralised DashScope API integration.

All prompts are designed to return ONLY valid JSON so responses can be
parsed directly without regex cleanup.
"""

import json
import logging
import re

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

DASHSCOPE_URL = (
    "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
)


class QwenAIService:
    """Thin, synchronous wrapper around the Qwen / DashScope text generation API."""

    def __init__(self):
        self.api_key = settings.DASHSCOPE_API_KEY
        if not self.api_key:
            # Log a warning but don't hard-crash — the HTTP call will fail
            # with a clear 401 that surfaces in logs.
            logger.warning(
                "DASHSCOPE_API_KEY is not set in settings. "
                "AI calls will return 401 from DashScope."
            )
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def parse_conditions(self, raw_text: str) -> dict:
        """
        Converts plain-language escrow conditions into a structured
        list of milestones.

        Returns:
            {
                "milestones": [
                    {
                        "description": str,
                        "verifiable": bool,
                        "deadline_hint": str | null
                    },
                    ...
                ]
            }
        """
        prompt = f"""You are a legal assistant for an escrow platform.
Parse the following transaction conditions into a structured JSON object.
The object must have a single key "milestones" whose value is a list of milestone objects.
Each milestone object must have exactly these keys:
  - description (str): clear, actionable description of what must happen
  - verifiable (bool): can completion be objectively confirmed?
  - deadline_hint (str or null): any time reference mentioned, else null

Return ONLY the raw JSON object — no markdown, no explanation.

Conditions:
{raw_text}"""
        return self._call(prompt)

    def verify_proof(self, milestone_description: str, proof_description: str) -> dict:
        """
        Assesses whether submitted proof satisfies a milestone.

        Returns:
            {"satisfied": bool, "confidence": int (0-100), "reason": str}
        """
        prompt = f"""You are an impartial escrow adjudicator.
Milestone: {milestone_description}
Submitted proof: {proof_description}

Does the proof satisfy the milestone?
Return ONLY a raw JSON object with keys: satisfied (bool), confidence (0-100 int), reason (str).
No markdown, no explanation."""
        return self._call(prompt)

    def resolve_dispute(
        self,
        agreement_summary: str,
        buyer_claim: str,
        seller_claim: str,
    ) -> dict:
        """
        Renders a dispute verdict.

        Returns:
            {
                "ruling": "buyer" | "seller" | "split",
                "split_ratio": "50%:50%" | null,
                "reasoning": str
            }
        """
        prompt = f"""You are a neutral dispute arbitrator for a digital escrow platform.
Agreement summary: {agreement_summary}
Buyer's claim: {buyer_claim}
Seller's evidence: {seller_claim}

Render a fair, reasoned verdict.
Return ONLY a raw JSON object with keys:
  - ruling: one of "buyer", "seller", "split"
  - split_ratio: a string like "70%:30%" (buyer%:seller%) if ruling is "split", else null
  - reasoning: str explaining the verdict

No markdown, no explanation."""
        return self._call(prompt)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call(self, prompt: str) -> dict:
        """Posts a single-turn message to the Qwen API and returns parsed JSON."""
        payload = {
            "model": "qwen-plus",
            "input": {
                "messages": [{"role": "user", "content": prompt}]
            },
            "parameters": {"result_format": "message"},
        }

        try:
            response = httpx.post(
                DASHSCOPE_URL,
                json=payload,
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "DashScope HTTP error: status=%s body=%s",
                exc.response.status_code,
                exc.response.text,
            )
            raise
        except httpx.TimeoutException as exc:
            logger.error("DashScope request timed out: %s", exc)
            raise
        except httpx.RequestError as exc:
            logger.error("DashScope connection error: %s", exc)
            raise

        # --- Parse the response body ---
        body = response.json()
        try:
            raw_content = body["output"]["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            # Log the full response so the real cause (quota error, model error,
            # unexpected schema change) is visible in server logs.
            logger.error(
                "DashScope response has unexpected shape: %s — full body: %s",
                exc,
                body,
            )
            raise ValueError(
                f"DashScope returned an unexpected response structure: {body}"
            ) from exc

        # Strip accidental markdown fences that some model versions add.
        # NOTE: str.strip() strips *characters*, not substrings, so we must
        # use re.sub to correctly remove ```json ... ``` wrappers.
        clean = re.sub(r"^```(?:json)?\s*", "", raw_content.strip(), flags=re.IGNORECASE)
        clean = re.sub(r"\s*```$", "", clean).strip()

        try:
            return json.loads(clean)
        except json.JSONDecodeError as exc:
            logger.error(
                "Qwen returned non-JSON content (after fence strip): %r", raw_content
            )
            raise ValueError(f"Qwen response was not valid JSON: {raw_content}") from exc
