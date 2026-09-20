mod actions;
mod config;
mod engine;
mod ui;

use anyhow::Result;
use clap::Parser;
use std::path::PathBuf;

#[derive(Parser, Debug)]
#[command(version = concat!(env!("CARGO_PKG_VERSION"), " (", env!("TCTL_REVISION"), ")"), about = "Docker fleet console. Run without a command for the TUI.")]
pub struct Cli {
    #[arg(
        long,
        global = true,
        help = "Repository root; otherwise discovered from cwd or binary"
    )]
    root: Option<PathBuf>,
    #[arg(long, global = true, help = "Confirm mutations in noninteractive use")]
    yes: bool,
    #[arg(long, global = true)]
    force: bool,
    #[arg(long, global = true, default_value_t = 100)]
    lines: u32,
    #[arg(long, global = true)]
    follow: bool,
    #[arg(long, global = true)]
    replicas: Option<u32>,
    #[arg(value_enum)]
    action: Option<actions::Action>,
    /// Stack, alias (ms/nx/sg), or stack/service. Deploy also accepts a fleet-wide service.
    target: Option<String>,
    services: Vec<String>,
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    let repo = config::Repo::load(cli.root.as_deref())?;
    match cli.action {
        Some(action) => tokio::select! {
            result = actions::run_cli(&repo, &cli, action) => result,
            _ = tokio::signal::ctrl_c() => anyhow::bail!("Canceled. Remote changes may already have applied; inspect before retrying."),
        },
        None => ui::run(repo).await,
    }
}
