use axum::{
    Router,
    body::Body,
    http::{Request, StatusCode},
};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::postgres::PgPoolOptions;
use std::time::Duration;
use tower::ServiceExt;

async fn get(app: Router, path: &str) -> (StatusCode, Value) {
    let response = app
        .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    let body = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, body)
}

fn disconnected_pool() -> sqlx::PgPool {
    PgPoolOptions::new()
        .acquire_timeout(Duration::from_millis(50))
        .connect_lazy("postgres://test:test@127.0.0.1:1/unavailable")
        .unwrap()
}

#[tokio::test]
async fn liveness_returns_revision_without_needing_database() {
    let (status, body) = get(
        app_api::router(disconnected_pool(), "revision-123".into()),
        "/health",
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        body,
        json!({"status": "healthy", "revision": "revision-123"})
    );
}

#[tokio::test]
async fn readiness_fails_closed_without_leaking_database_details() {
    let (status, body) = get(
        app_api::router(disconnected_pool(), "revision-456".into()),
        "/health/db",
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(
        body,
        json!({"status": "unhealthy", "revision": "revision-456"})
    );
}

#[tokio::test]
#[ignore = "requires an isolated PostgreSQL database in TEST_DATABASE_URL"]
async fn migrations_are_repeatable_and_readiness_checks_real_postgres() {
    let url = std::env::var("TEST_DATABASE_URL")
        .expect("set TEST_DATABASE_URL to an isolated test database");
    let pool = PgPoolOptions::new().connect(&url).await.unwrap();
    app_api::migrate(&pool).await.unwrap();
    app_api::migrate(&pool).await.unwrap();
    let migrations: i64 = sqlx::query_scalar("SELECT count(*) FROM _sqlx_migrations WHERE success")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert!(
        migrations >= 1,
        "startup must apply the embedded SQLx migrations"
    );
    let (status, body) = get(
        app_api::router(pool.clone(), "database-revision".into()),
        "/health/db",
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        body,
        json!({"status": "healthy", "revision": "database-revision"})
    );
    pool.close().await;
}
