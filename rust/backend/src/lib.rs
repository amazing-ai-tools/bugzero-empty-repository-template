use axum::{Json, Router, extract::State, http::StatusCode, routing::get};
use serde::Serialize;
use sqlx::PgPool;

#[derive(Clone)]
struct AppState {
    pool: PgPool,
    revision: String,
}

#[derive(Serialize)]
struct Health {
    status: &'static str,
    revision: String,
}

pub fn router(pool: PgPool, revision: String) -> Router {
    Router::new()
        .route("/health", get(liveness))
        .route("/health/db", get(readiness))
        .with_state(AppState { pool, revision })
}

pub async fn migrate(pool: &PgPool) -> Result<(), sqlx::migrate::MigrateError> {
    sqlx::migrate!("./migrations").run(pool).await
}

async fn liveness(State(state): State<AppState>) -> Json<Health> {
    Json(Health {
        status: "healthy",
        revision: state.revision,
    })
}

async fn readiness(State(state): State<AppState>) -> (StatusCode, Json<Health>) {
    let healthy = sqlx::query_scalar::<_, i32>("SELECT 1")
        .fetch_one(&state.pool)
        .await
        .is_ok();
    let (code, status) = if healthy {
        (StatusCode::OK, "healthy")
    } else {
        (StatusCode::SERVICE_UNAVAILABLE, "unhealthy")
    };
    (
        code,
        Json(Health {
            status,
            revision: state.revision,
        }),
    )
}
