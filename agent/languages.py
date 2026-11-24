"""Country-aware language signals for student-portal discovery.

These terms rank public links and guide web search. They are not a substitute
for verification: every selected endpoint must still be live, HTTPS, show a
credential form, and carry student/academic evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageProfile:
    languages: str
    portal_terms: tuple[str, ...]
    search_terms: tuple[str, ...]


_EN = LanguageProfile("English", (), ("student portal", "student login", "LMS"))

PROFILES: dict[str, LanguageProfile] = {
    "japan": LanguageProfile("Japanese", ("ログイン", "サインイン", "認証", "学生", "学務", "履修", "授業", "学習管理", "教務", "ポータル"), ("学生ポータル", "学生 ログイン", "学習管理システム", "認証")),
    "taiwan": LanguageProfile("Traditional Chinese", ("登入", "登錄", "學生", "學習管理", "教務", "校務", "選課", "學生入口"), ("學生入口", "學生 登入", "學習管理系統", "校務系統")),
    "hong kong": LanguageProfile("Traditional Chinese and English", ("登入", "登錄", "學生", "學習管理", "教務", "校務", "學生入口"), ("學生入口", "學生 登入", "學習管理系統", "student portal")),
    "united arab emirates": LanguageProfile("Arabic and English", ("تسجيل الدخول", "بوابة الطالب", "الطلاب", "نظام إدارة التعلم", "نظام التعليم"), ("بوابة الطالب", "تسجيل دخول الطلاب", "نظام إدارة التعلم")),
    "iraq": LanguageProfile("Arabic", ("تسجيل الدخول", "بوابة الطالب", "الطلاب", "نظام إدارة التعلم", "نظام التعليم"), ("بوابة الطالب", "تسجيل دخول الطلاب", "نظام إدارة التعلم")),
    "jordan": LanguageProfile("Arabic", ("تسجيل الدخول", "بوابة الطالب", "الطلاب", "نظام إدارة التعلم", "نظام التعليم"), ("بوابة الطالب", "تسجيل دخول الطلاب", "نظام إدارة التعلم")),
    "turkey": LanguageProfile("Turkish", ("giriş", "öğrenci", "öğrenci bilgi sistemi", "uzaktan eğitim"), ("öğrenci bilgi sistemi", "öğrenci girişi", "uzaktan eğitim")),
    "russia": LanguageProfile("Russian", ("вход", "студент", "личный кабинет", "электронное обучение"), ("личный кабинет студента", "вход студента", "электронное обучение")),
    "nepal": LanguageProfile("Nepali and English", ("लगइन", "विद्यार्थी", "छात्र", "विद्यार्थी पोर्टल", "सिकाइ"), ("विद्यार्थी पोर्टल", "विद्यार्थी लगइन", "student portal")),
    "france": LanguageProfile("French", ("connexion", "se connecter", "étudiant", "espace étudiant", "portail étudiant"), ("portail étudiant", "connexion étudiant", "espace numérique de travail")),
    "senegal": LanguageProfile("French", ("connexion", "étudiant", "espace étudiant", "portail étudiant"), ("portail étudiant", "connexion étudiant")),
    "ivory coast": LanguageProfile("French", ("connexion", "étudiant", "espace étudiant", "portail étudiant"), ("portail étudiant", "connexion étudiant")),
    "india": LanguageProfile("English and Indian languages", ("लॉगिन", "छात्र", "विद्यार्थी", "छात्र पोर्टल", "शिक्षा"), ("student portal", "छात्र पोर्टल", "विद्यार्थी लॉगिन")),
    "bolivia": LanguageProfile("Spanish", ("iniciar sesión", "estudiante", "portal estudiantil", "aula virtual"), ("portal estudiantil", "iniciar sesión estudiante", "aula virtual")),
}


def profile_for_country(country: str) -> LanguageProfile:
    return PROFILES.get((country or "").strip().lower(), _EN)


# --------------------------------------------------------------------------- #
#  Student-audience evidence for the final login gate                          #
# --------------------------------------------------------------------------- #
# `PROFILES` above cannot serve as audience evidence: its portal terms mix
# sign-in verbs ("connexion", "giriş", "вход") with student nouns, and it is
# keyed by a handful of countries. The gate has no country to key on and must
# not treat "log in" as proof of who is logging in, so the audience vocabulary
# is kept here by LANGUAGE and matched all at once.
#
# Every entry is a regex fragment (compiled case-insensitively) naming one of:
#   * the student            estudiante · aluno · sinh viên · mahasiswa
#   * a student identifier   matrícula · legajo · MSSV · NIM
#   * an academic service    control de estudios · đào tạo · ambiente virtual
#                            de aprendizagem
# Institution words ("universidad", "faculté") are deliberately absent: they
# prove ownership, not audience — the same reason the English list omits
# "university". Staff and alumni words are absent too.
#
# Distinctive stems carry no word boundary so they also match inside form-field
# names (`txtMaSinhVien`, `inputLegajo`); short or ambiguous tokens are bounded.

#: "ex-alumnos" / "antigos alunos" are alumni, not students.
_NOT_FORMER = r"(?<!ex)(?<!ex[\s-])(?<!antiguos\s)(?<!antigos\s)"

STUDENT_AUDIENCE_PATTERNS: tuple[str, ...] = (
    # Spanish
    r"estudiant(?:es?|il(?:es)?|ado)",
    # Not "alumna": English alumni pages use that singular.
    _NOT_FORMER + r"alumn(?:os?|as|ado)",
    r"matr[ií]cula",                    # also Portuguese
    r"legajo",                          # Argentine student file number
    r"acad[eé]mic[oa]s?",
    r"asignaturas?",
    r"(?:control|plan)\s+de\s+estudios",
    r"(?:aula|campus)\s+virtual",
    r"entorno\s+virtual\s+de\s+aprendizaje",
    r"educaci[oó]n\s+a\s+distancia",
    # Portuguese
    r"estudant(?:es?|il)",
    _NOT_FORMER + r"alun[oa]s?",
    r"discentes?",
    r"acad[eê]mic[oa]s?",
    r"ambiente\s+virtual\s+de\s+(?:ensino\s+e\s+)?aprendiza(?:gem|do)",
    r"sala\s+de\s+aula",
    r"(?:ensino|educa[cç][aã]o)\s+a\s+dist[aâ]ncia",
    # Vietnamese
    r"sinh\s*vi[eê]n",                  # sinh viên · sinhvien
    r"học\s+viên",
    r"\bmssv\b",                        # mã số sinh viên
    r"đào\s+tạo", r"\bdaotao\b",        # academic affairs / training office
    r"tín\s+chỉ", r"\btinchi\b",        # credit-registration portals
    r"học\s+phần",
    # French
    r"[eé]tudiant",
    r"\bélèves?\b",
    r"\bapprenants?\b",
    r"scolarit[eé]",
    r"espace\s+num[eé]rique\s+de\s+travail",
    # Indonesian and Malay
    r"mahasisw[ai]",
    r"\bsiswa\b",
    r"\bpelajar\b",
    r"\bnim\b",                         # nomor induk mahasiswa
    r"siakad",                          # sistem informasi akademik
    r"akademik",                        # also Turkish
    r"\b(?:per)?kuliah(?:an)?\b",
    # Turkish
    r"[oö][gğ]renc[iıİ]",               # öğrenci · ogrenci · ÖĞRENCİ
    r"uzaktan\s+e[gğ]itim",
    # Student nouns the discovery profiles above already rank on, so a link the
    # ranker promotes is not then rejected by the gate for lack of evidence.
    # Traditional Chinese
    "學生", "學號", "選課", "教務",
    # Arabic
    "الطالب", "الطلاب", "الطلبة",
    # Russian
    "студент",
    # Hindi and Nepali
    "छात्र", "विद्यार्थी",
)

#: One alternation over every language above. Match against NFC-normalised
#: text: Vietnamese pages in particular ship decomposed diacritics.
STUDENT_AUDIENCE_RE = re.compile(
    "|".join(f"(?:{fragment})" for fragment in STUDENT_AUDIENCE_PATTERNS),
    re.I,
)


# --------------------------------------------------------------------------- #
#  Sign-in wording                                                             #
# --------------------------------------------------------------------------- #
# The words a page puts ON its sign-in control, per language. The gate uses
# them to recognise a Login link or button on a page that has no password form
# of its own. English and Japanese live in `agent.guardrails._LOGIN_WORD_RE`.
#
# Only verbs and nouns that mean "sign in" belong here. Words for "access" or
# "entry" in general are left out or fenced, because the same word names
# things that are not a login:
#   * Portuguese "acesso" heads "Acesso à informação" on every public site.
#   * Spanish "ingreso" and Indonesian "jalur masuk" mean ADMISSION intake.
#   * Turkish "girişim(cilik)" is entrepreneurship; `\b` keeps "giriş" apart.
LOGIN_WORD_PATTERNS: tuple[str, ...] = (
    # Portuguese
    r"\bentrar\b",                      # also Spanish
    r"\bacessar\b",
    r"iniciar\s+sess[aã]o",
    r"fazer\s+login",
    # Spanish
    r"inici(?:ar|o\s+de)\s+sesi[oó]n",
    r"\bingresar\b",
    r"\bacceder\b",
    r"\bidentificarse\b",
    # French
    r"\bconnexion\b", r"se\s+connecter", r"s['’]identifier",
    # Turkish
    r"\bgiri[şs]i?\b",                  # giriş · girişi · giris
    r"oturum\s+a[cç]",
    # Indonesian and Malay
    r"log\s+masuk",
    r"(?<!jalur\s)(?<!seleksi\s)(?<!ujian\s)(?<!tes\s)\bmasuk\b",
    # Vietnamese
    r"đăng\s+nhập", r"\bdang\s*nhap\b",
    # Russian
    r"\bвойти\b", r"\bвход\b", r"авторизаци[яи]",
    # Hindi and Nepali
    "लॉगिन", "लॉग इन", "साइन इन",
    # Arabic
    "تسجيل الدخول", "دخول",
    # Chinese
    "登录", "登入", "登錄",
)

#: One alternation over every language above. Match against NFC-normalised text.
LOGIN_WORD_RE = re.compile(
    "|".join(f"(?:{fragment})" for fragment in LOGIN_WORD_PATTERNS),
    re.I,
)
