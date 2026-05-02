<!--
Prompt for Stage C.2 (T&C Analyzer).

Purpose: given the scraped Terms & Conditions text of a university student
login portal, ask Claude for a structured verdict on whether data sharing by
a student or a third party is permitted.

Expected output schema (JSON):
  {
    "verdict":   "Yes" | "Maybe" | "No",
    "evidence":  "<verbatim quote(s) from the T&C>",
    "reasoning": "<one-paragraph explanation>"
  }

TODO: fill in the actual prompt in a follow-up message.
-->
