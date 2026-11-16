use app_web::i18n::{HealthState, Locale, resolve_locale};
use gloo_net::http::Request;
use leptos::prelude::*;
use serde::Deserialize;

#[derive(Deserialize)]
struct AppConfig {
    #[serde(alias = "name")]
    display_name: String,
    #[serde(default)]
    bugzero_app_key: String,
    #[serde(default)]
    bugzero_widget_url: String,
}

#[derive(Deserialize)]
struct Health {
    status: String,
    revision: String,
}

#[component]
fn App() -> impl IntoView {
    let (locale, set_locale) = signal(initial_locale());
    let (name, set_name) = signal(None::<String>);
    let (status, set_status) = signal(HealthState::Connecting);
    let (revision, set_revision) = signal(String::new());
    let display_name = move || {
        name.get()
            .unwrap_or_else(|| locale.get().copy().app_name.to_owned())
    };

    Effect::new(move |_| {
        let current = locale.get();
        if let Some(document) = web_sys::window().and_then(|window| window.document()) {
            if let Some(root) = document.document_element() {
                let _ = root.set_attribute("lang", current.code());
            }
            document.set_title(&display_name());
        }
    });

    leptos::task::spawn_local(async move {
        if let Ok(response) = Request::get("/app-config.json").send().await
            && response.ok()
            && let Ok(config) = response.json::<AppConfig>().await
        {
            install_widget(&config);
            if !config.display_name.trim().is_empty() {
                set_name.set(Some(config.display_name));
            }
        }

        match Request::get("/api/health/db").send().await {
            Ok(response) if response.ok() => match response.json::<Health>().await {
                Ok(health) if health.status == "healthy" => {
                    set_status.set(HealthState::Ready);
                    set_revision.set(health.revision);
                }
                _ => set_status.set(HealthState::Starting),
            },
            _ => set_status.set(HealthState::Starting),
        }
    });

    view! {
        <div class="app-shell">
        <header class="page-header">
            <div class="language-picker">
                <label for="app-language">{move || locale.get().copy().language}</label>
                <select id="app-language"
                    prop:value=move || locale.get().code()
                    on:change=move |event| {
                        if let Some(value) = Locale::parse(&event_target_value(&event)) {
                            set_locale.set(value);
                            save_locale(value);
                        }
                    }
                >
                    <option value="en" lang="en">"English"</option>
                    <option value="fr" lang="fr">"Français"</option>
                    <option value="pt" lang="pt">"Português"</option>
                </select>
            </div>
        </header>
        <main>
            <p class="eyebrow">{move || locale.get().copy().eyebrow}</p>
            <h1>{display_name}</h1>
            <p class="intro">{move || locale.get().copy().intro}</p>
            <section class="status" aria-live="polite">
                <span class="indicator" class:ready=move || status.get() == HealthState::Ready aria-hidden="true"></span>
                <p>{move || locale.get().status(status.get())}</p>
            </section>
            <p class="revision">{move || {
                let value = revision.get();
                if value.is_empty() { String::new() } else { format!("{} {value}", locale.get().copy().revision) }
            }}</p>
        </main>
        </div>
    }
}

const LANGUAGE_STORAGE_KEY: &str = "app-language";

fn initial_locale() -> Locale {
    let Some(window) = web_sys::window() else {
        return Locale::En;
    };
    let saved = window
        .local_storage()
        .ok()
        .flatten()
        .and_then(|storage| storage.get_item(LANGUAGE_STORAGE_KEY).ok().flatten());
    let languages: Vec<String> = window
        .navigator()
        .languages()
        .iter()
        .filter_map(|value| value.as_string())
        .collect();
    let languages: Vec<&str> = languages.iter().map(String::as_str).collect();
    resolve_locale(saved.as_deref(), &languages)
}

fn save_locale(locale: Locale) {
    if let Some(storage) =
        web_sys::window().and_then(|window| window.local_storage().ok().flatten())
    {
        let _ = storage.set_item(LANGUAGE_STORAGE_KEY, locale.code());
    }
}

fn install_widget(config: &AppConfig) {
    if config.bugzero_app_key.trim().is_empty() {
        return;
    }
    let Some(url) = app_web::widget_url(&config.bugzero_widget_url) else {
        return;
    };
    let Some(document) = leptos::web_sys::window().and_then(|window| window.document()) else {
        return;
    };
    if document
        .get_element_by_id("bugzero-widget-script")
        .is_some()
    {
        return;
    }
    let Some(body) = document.body() else {
        return;
    };
    let Ok(script) = document.create_element("script") else {
        return;
    };
    for (name, value) in [
        ("id", "bugzero-widget-script"),
        ("src", url.as_str()),
        ("data-app-key", config.bugzero_app_key.as_str()),
        ("data-api-url", "https://api.bugzero.amazing-ai.tools"),
        ("crossorigin", "anonymous"),
        ("async", ""),
    ] {
        if script.set_attribute(name, value).is_err() {
            return;
        }
    }
    let _ = body.append_child(&script);
}

fn main() {
    leptos::mount::mount_to_body(App);
}
