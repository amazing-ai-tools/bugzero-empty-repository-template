pub mod i18n;

pub fn widget_url(value: &str) -> Option<url::Url> {
    let url = url::Url::parse(value).ok()?;
    (url.scheme() == "https"
        && url.host_str().is_some()
        && url.username().is_empty()
        && url.password().is_none())
    .then_some(url)
}

#[cfg(test)]
mod tests {
    use super::widget_url;

    #[test]
    fn permits_the_https_widget_url() {
        let value = "https://bugzero.amazing-ai.tools/widget.js";
        assert_eq!(widget_url(value).unwrap().as_str(), value);
    }

    #[test]
    fn rejects_script_urls_and_insecure_or_credential_bearing_sources() {
        for value in [
            "javascript:alert(1)",
            "data:text/javascript,alert(1)",
            "http://bugzero.amazing-ai.tools/widget.js",
            "https://user:secret@example.com/widget.js",
            "/widget.js",
            "",
        ] {
            assert!(
                widget_url(value).is_none(),
                "unexpected script source: {value}"
            );
        }
    }
}
