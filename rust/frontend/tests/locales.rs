use app_web::i18n::{HealthState, Locale, resolve_locale};

#[test]
fn saved_choice_wins_over_browser_languages() {
    assert_eq!(resolve_locale(Some("fr"), &["pt-BR", "en-US"]), Locale::Fr);
}

#[test]
fn browser_preferences_use_the_first_supported_language_and_region() {
    assert_eq!(resolve_locale(None, &["de-DE", "pt-BR", "fr"]), Locale::Pt);
    assert_eq!(resolve_locale(Some("invalid"), &["FR-ca"]), Locale::Fr);
    assert_eq!(resolve_locale(None, &["en-GB", "fr"]), Locale::En);
}

#[test]
fn unsupported_or_missing_preferences_fall_back_to_english() {
    assert_eq!(resolve_locale(None, &[]), Locale::En);
    assert_eq!(resolve_locale(Some("es"), &["de", "es"]), Locale::En);
    assert_eq!(Locale::parse("french"), None);
    assert_eq!(Locale::parse("pt_BR"), None);
}

#[test]
fn all_template_text_and_health_states_are_localized() {
    let expected = [
        (
            Locale::En,
            "en",
            "Your app",
            "Language",
            "Built with Rust",
            "A fresh start for your next idea.",
            "Connecting to your app…",
            "Your app is ready.",
            "Your app is starting. Please refresh in a moment.",
            "Revision",
        ),
        (
            Locale::Fr,
            "fr",
            "Votre application",
            "Langue",
            "Créé avec Rust",
            "Un nouveau départ pour votre prochaine idée.",
            "Connexion à votre application…",
            "Votre application est prête.",
            "Votre application démarre. Veuillez actualiser la page dans un instant.",
            "Révision",
        ),
        (
            Locale::Pt,
            "pt",
            "Seu aplicativo",
            "Idioma",
            "Feito com Rust",
            "Um novo começo para sua próxima ideia.",
            "Conectando ao seu aplicativo…",
            "Seu aplicativo está pronto.",
            "Seu aplicativo está iniciando. Atualize a página em instantes.",
            "Revisão",
        ),
    ];
    for (locale, code, name, label, eyebrow, intro, connecting, ready, starting, revision) in
        expected
    {
        let copy = locale.copy();
        assert_eq!(locale.code(), code);
        assert_eq!(copy.app_name, name);
        assert_eq!(copy.language, label);
        assert_eq!(copy.eyebrow, eyebrow);
        assert_eq!(copy.intro, intro);
        assert_eq!(locale.status(HealthState::Connecting), connecting);
        assert_eq!(locale.status(HealthState::Ready), ready);
        assert_eq!(locale.status(HealthState::Starting), starting);
        assert_eq!(copy.revision, revision);
    }
}
