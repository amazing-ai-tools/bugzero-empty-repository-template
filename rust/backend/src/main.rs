use sqlx::postgres::PgPoolOptions;
use std::{error::Error, net::SocketAddr, time::Duration};
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();

    let args: Vec<String> = std::env::args().skip(1).collect();
    let migrate_only = match args.as_slice() {
        [] => false,
        [arg] if arg == "--migrate-only" => true,
        _ => return Err("usage: app-api [--migrate-only]".into()),
    };
    let database_url = std::env::var("DATABASE_URL").map_err(|_| "DATABASE_URL is required")?;
    let pool = PgPoolOptions::new()
        .max_connections(10)
        .acquire_timeout(Duration::from_secs(3))
        .connect(&database_url)
        .await?;
    app_api::migrate(&pool).await?;
    if migrate_only {
        tracing::info!("database migrations applied");
        pool.close().await;
        return Ok(());
    }

    let bind_addr: SocketAddr = std::env::var("BIND_ADDR")
        .unwrap_or_else(|_| "0.0.0.0:8000".into())
        .parse()?;
    let revision = std::env::var("APP_REVISION")
        .unwrap_or_else(|_| option_env!("APP_REVISION").unwrap_or("development").into());
    let listener = tokio::net::TcpListener::bind(bind_addr).await?;
    tracing::info!(address = %listener.local_addr()?, revision, "app API listening");
    axum::serve(listener, app_api::router(pool.clone(), revision))
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    pool.close().await;
    Ok(())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("install Ctrl+C handler");
    };
    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("install SIGTERM handler")
            .recv()
            .await;
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        () = ctrl_c => {},
        () = terminate => {},
    }
}
