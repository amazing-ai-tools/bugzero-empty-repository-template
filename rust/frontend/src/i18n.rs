//! Complete, compile-time checked copy for the starter interface.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Locale {
    En,
    Fr,
    Pt,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HealthState {
    Connecting,
    Ready,
    Starting,
}

pub struct Copy {
    pub app_name: &'static str,
    pub language: &'static str,
    pub eyebrow: &'static str,
    pub intro: &'static str,
    pub connecting: &'static str,
    pub ready: &'static str,
    pub starting: &'static str,
    pub revision: &'static str,
}

impl Locale {
    pub fn parse(value: &str) -> Option<Self> {
        match value.split('-').next()?.to_ascii_lowercase().as_str() {
            "en" => Some(Self::En),
            "fr" => Some(Self::Fr),
            "pt" => Some(Self::Pt),
            _ => None,
        }
    }

    pub fn code(self) -> &'static str {
        match self {
            Self::En => "en",
            Self::Fr => "fr",
            Self::Pt => "pt",
        }
    }

    pub fn copy(self) -> Copy {
        match self {
            Self::En => Copy {
                app_name: "Your app",
                language: "Language",
                eyebrow: "Built with Rust",
                intro: "A fresh start for your next idea.",
                connecting: "Connecting to your app…",
                ready: "Your app is ready.",
                starting: "Your app is starting. Please refresh in a moment.",
                revision: "Revision",
            },
            Self::Fr => Copy {
                app_name: "Votre application",
                language: "Langue",
                eyebrow: "Créé avec Rust",
                intro: "Un nouveau départ pour votre prochaine idée.",
                connecting: "Connexion à votre application…",
                ready: "Votre application est prête.",
                starting: "Votre application démarre. Veuillez actualiser la page dans un instant.",
                revision: "Révision",
            },
            Self::Pt => Copy {
                app_name: "Seu aplicativo",
                language: "Idioma",
                eyebrow: "Feito com Rust",
                intro: "Um novo começo para sua próxima ideia.",
                connecting: "Conectando ao seu aplicativo…",
                ready: "Seu aplicativo está pronto.",
                starting: "Seu aplicativo está iniciando. Atualize a página em instantes.",
                revision: "Revisão",
            },
        }
    }

    pub fn status(self, state: HealthState) -> &'static str {
        let copy = self.copy();
        match state {
            HealthState::Connecting => copy.connecting,
            HealthState::Ready => copy.ready,
            HealthState::Starting => copy.starting,
        }
    }
}

pub fn resolve_locale(saved: Option<&str>, browser_languages: &[&str]) -> Locale {
    saved
        .and_then(Locale::parse)
        .or_else(|| {
            browser_languages
                .iter()
                .find_map(|value| Locale::parse(value))
        })
        .unwrap_or(Locale::En)
}
