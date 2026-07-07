// Country → flag emoji. Known countries mapped explicitly; anything else
// falls back to a globe so new countries still render cleanly.
const COUNTRY_FLAGS = {
  India: "🇮🇳",
  Bangladesh: "🇧🇩",
  Indonesia: "🇮🇩",
  Argentina: "🇦🇷",
  Pakistan: "🇵🇰",
  Nepal: "🇳🇵",
  "Sri Lanka": "🇱🇰",
  Philippines: "🇵🇭",
  Brazil: "🇧🇷",
  Mexico: "🇲🇽",
  Nigeria: "🇳🇬",
};

export const countryFlag = (name) => COUNTRY_FLAGS[name] || "🌐";
