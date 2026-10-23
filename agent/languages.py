"""Country-aware language signals for student-portal discovery.

These terms rank public links and guide web search. They are not a substitute
for verification: every selected endpoint must still be live, HTTPS, show a
credential form, and carry student/academic evidence.
"""
from __future__ import annotations

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

