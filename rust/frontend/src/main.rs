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
    let (name, set_name) = signal("Your app".to_owned());
    let (status, set_status) = signal("Connecting to your app…".to_owned());
    let (ready, set_ready) = signal(false);
    let (revision, set_revision) = signal(String::new());

    leptos::task::spawn_local(async move {
        if let Ok(response) = Request::get("/app-config.json").send().await
            && response.ok()
            && let Ok(config) = response.json::<AppConfig>().await
        {
            install_widget(&config);
            if !config.display_name.trim().is_empty() {
                if let Some(document) = leptos::web_sys::window().and_then(|w| w.document()) {
                    document.set_title(&config.display_name);
                }
                set_name.set(config.display_name);
            }
        }

        match Request::get("/api/health/db").send().await {
            Ok(response) if response.ok() => match response.json::<Health>().await {
                Ok(health) if health.status == "healthy" => {
                    set_ready.set(true);
                    set_status.set("Your app is ready.".into());
                    set_revision.set(health.revision);
                }
                _ => set_status.set("Your app is starting. Please refresh in a moment.".into()),
            },
            _ => set_status.set("Your app is starting. Please refresh in a moment.".into()),
        }
    });

    view! {
        <main>
            <p class="eyebrow">"Built with Rust"</p>
            <h1>{name}</h1>
            <p class="intro">"A fresh start for your next idea."</p>
            <section class="status" aria-live="polite">
                <span class="indicator" class:ready=ready aria-hidden="true"></span>
                <p>{status}</p>
            </section>
            <p class="revision">{move || {
                let value = revision.get();
                if value.is_empty() { String::new() } else { format!("Revision {value}") }
            }}</p>
        </main>
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
