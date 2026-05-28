"""模組 C：呼叫 LLM 將多源文字萃取為結構化時間軸 JSON。

設計重點：
- System Prompt 用英文（目標讀者 Blackswan 讀英文）
- 用 Anthropic Tool Use 強制 JSON Schema，避免 LLM 自由發揮破格式
- 多個 source 一次送進去，讓 LLM 能跨來源 dedupe
"""
from __future__ import annotations
import os
import sys
from anthropic import Anthropic

SYSTEM_PROMPT = """You are a true-crime timeline extraction specialist. You read source materials (news articles, court documents, encyclopedia entries) about a single criminal case and produce a structured chronological timeline of every dateable event.

OUTPUT REQUIREMENTS:
- Output ONLY by calling the `submit_timeline` tool. No commentary, no explanations, no preamble.
- Every event MUST have all four fields populated: Date, Event, Phase, Source_Ref.

FIELD RULES:
- Date: Use YYYY-MM-DD when the day is known, YYYY-MM when only month+year are known, YYYY when only year is known. NEVER invent precision you do not have.
- Event: Brief English description, MAXIMUM 30 WORDS. Be specific (names, locations, what happened). No speculation.
- Phase: Exactly one of:
    * "Pre-incident" — perpetrator/victim background, births, upbringing, how they met, pre-crime tensions, mental health history.
    * "Incident" — events during the active commission of the crime itself.
    * "Investigation" — body discovery, evidence collection, arrest, interrogation, indictment, pre-trial procedure.
    * "Trial" — arraignment, trial proceedings, conviction, sentencing of any defendant.
    * "Post-trial" — appeals, prison events, parole hearings, executions, post-conviction legal challenges, requests for clemency.
- Source_Ref: Each source block in the user message is labeled `=== SOURCE: <ref> ===`. Use that exact `<ref>` value verbatim.

EXTRACTION RULES:
1. Extract perpetrator and victim birth dates whenever mentioned, even if only the year is known.
2. Extract every distinct dateable event — do not skip routine procedural milestones.
3. Do NOT extract general statements that have no date anchor (e.g., "her childhood was difficult" without a date).
4. DEDUPLICATION — apply these checks IN ORDER when MULTIPLE entries describe the SAME underlying event:
   (a) PRECISION MERGE: If two candidate entries describe the same event but with different date precisions (e.g., "2022-11" vs "2022-11-18", or "1976" vs "1976-03-10"), keep ONLY the entry with the more precise date. Discard the less precise one. This is critical when an explicit precise date exists in one source and a year-only or month-only mention exists in another.
   (b) AUTHORITY SELECTION: When two entries describe the same event at equivalent precision but with different wording or sources, prefer this Source_Ref priority: court document > Wikipedia > established encyclopedia > news outlet > "user-pasted text".
   (c) CONFLICTING DATES: If a single source presents two different dates for the same event (e.g., one paragraph says "2003" and another says "2004-08-12"), output ONLY the more specific date.
5. NEVER invent dates. If text says "in late 1994" output Date "1994". If text says "third grade" without a year, do NOT include the event.
6. Sort the final output strictly by Date ascending (oldest first). Events with year-only dates sort before more precise dates within the same year.
7. Extract timeline events for EVERY named perpetrator/co-defendant individually, not just the primary defendant. This includes their separate trials, sentencings, parole hearings, prison incidents, and deaths in custody. Treat each co-defendant's procedural milestones as distinct events warranting their own entry.
8. BIRTH-YEAR INFERENCE FROM AGE:
   PRECONDITION (CRITICAL): Before applying this rule to any person, scan ALL source materials for an explicit birth date or birth year for that person (e.g., "born March 10, 1976" or "born in 1976"). If ANY explicit birth date or year exists in ANY source, USE that explicit value and SKIP this inference rule for that person entirely. Do NOT create both an explicit birth entry AND an inferred birth entry — that produces duplicates.
   APPLY ONLY WHEN: No explicit birth date or year exists for the person AND a source mentions their age at a dated event (e.g., "the 19-year-old Slemmer" referring to a January 12, 1995 murder; or "Tadaryl Shipp, then 17 years old" referring to the same crime).
   OUTPUT AS A RANGE: For "N years old at [date Y]", the birth year is EITHER (Y_year - N) or (Y_year - N - 1), depending on whether the birthday had passed by that date. This 2-year uncertainty is GENUINE and must NOT be papered over by picking a single year. Output BOTH endpoints:
       Date     = earlier possible year, i.e., (Y_year - N - 1), example "1977"
       Date_End = later possible year,   i.e., (Y_year - N),     example "1978"
   Concrete example for Tadaryl Shipp (no explicit birth date in any source, materials state "then 17 years old" at the January 12, 1995 murder):
       {"Date": "1977", "Date_End": "1978", "Event": "Tadaryl Shipp born (inferred from age 17 at January 1995 crime)", "Phase": "Pre-incident", "Source_Ref": "user-pasted text"}
   If, in a different case, materials give birthday month/day but NOT the year (rare), output a single Date without Date_End.
   EVENT FIELD FORMAT: `"<Name> born (inferred from age <N> at <event-context>)"`. Always include the "(inferred from age ...)" suffix so the inference is transparent and auditable.
   SCOPE: Apply this inference check to EVERY named perpetrator, co-defendant, AND victim. In multi-defendant cases, scan each individually — do not stop at the primary defendant.
   NO CHAINING: Inference must derive directly from explicit dated text; do not infer from already-inferred values.
"""

TOOL_SCHEMA = {
    "name": "submit_timeline",
    "description": "Submit the extracted chronological timeline of dateable events from the source materials.",
    "input_schema": {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "description": "Chronologically sorted timeline events (oldest first).",
                "items": {
                    "type": "object",
                    "properties": {
                        "Date": {
                            "type": "string",
                            "description": "Date in YYYY-MM-DD, YYYY-MM, or YYYY format. For inferred birth-year ranges (rule 8), this is the EARLIER endpoint.",
                        },
                        "Date_End": {
                            "type": "string",
                            "description": "OPTIONAL. Use ONLY for inferred birth-year ranges (rule 8) where age implies a 2-year window. Format: YYYY. Omit for all other events.",
                        },
                        "Event": {
                            "type": "string",
                            "description": "Brief English description, max 30 words.",
                        },
                        "Phase": {
                            "type": "string",
                            "enum": [
                                "Pre-incident",
                                "Incident",
                                "Investigation",
                                "Trial",
                                "Post-trial",
                            ],
                        },
                        "Source_Ref": {
                            "type": "string",
                            "description": "Source URL or 'user-pasted text'.",
                        },
                    },
                    "required": ["Date", "Event", "Phase", "Source_Ref"],
                },
            }
        },
        "required": ["events"],
    },
}


def build_user_message(sources: list[tuple[str, str]]) -> str:
    """將 (source_ref, body_text) 序列組為單一帶標籤的 user message。"""
    parts = ["Please extract the timeline from the following source materials.\n"]
    for ref, body in sources:
        parts.append(f"\n=== SOURCE: {ref} ===\n{body.strip()}\n")
    parts.append(
        "\nNow extract every dateable event via the `submit_timeline` tool. "
        "Return events sorted chronologically (oldest first)."
    )
    return "".join(parts)


def extract_timeline(
    sources: list[tuple[str, str]],
    model: str | None = None,
    max_retries: int = 1,
) -> list[dict]:
    """
    主入口：呼叫 LLM 取得結構化時間軸事件。
    sources: [(source_ref, body_text), ...]
    回傳: list of {Date, Event, Phase, Source_Ref}
    """
    if not sources:
        return []

    client = Anthropic(api_key=os.environ["LLM_API_KEY"])
    model = model or os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
    user_message = build_user_message(sources)

    for attempt in range(max_retries + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=8192,
                system=SYSTEM_PROMPT,
                tools=[TOOL_SCHEMA],
                tool_choice={"type": "tool", "name": "submit_timeline"},
                messages=[{"role": "user", "content": user_message}],
            )
            for block in response.content:
                if block.type == "tool_use" and block.name == "submit_timeline":
                    return block.input.get("events", [])
            print(
                f"  ⚠ LLM 未呼叫指定 tool (attempt {attempt + 1})",
                file=sys.stderr,
            )
        except Exception as e:
            print(
                f"  ⚠ LLM 呼叫失敗 (attempt {attempt + 1})：{e}",
                file=sys.stderr,
            )

    print("  ❌ LLM 萃取最終失敗，返回空 list", file=sys.stderr)
    return []


def deduplicate(events: list[dict]) -> list[dict]:
    """
    防禦性去重：以 (Date, Event 前 10 詞 lowercased) 作為 key。
    LLM 已被要求做跨來源 dedupe，這層是保險。
    """
    seen: dict[tuple[str, str], dict] = {}
    for evt in events:
        date = evt.get("Date", "")
        event_text = evt.get("Event", "")
        first_words = " ".join(w.lower() for w in event_text.split()[:10])
        key = (date, first_words)
        if key not in seen:
            seen[key] = evt
    return list(seen.values())
