use crate::{
    Cli,
    config::{Repo, safe_name, safe_path},
    engine::{Connection, snapshot},
};
use anyhow::{Context, Result, bail, ensure};
use bollard::query_parameters::*;
use clap::ValueEnum;
use futures_util::TryStreamExt;
use std::{
    io::{self, IsTerminal, Write},
    path::Path,
    process::Stdio,
    time::Duration,
};
use tokio::{io::AsyncWriteExt, process::Command};

#[derive(Clone, Copy, Debug, PartialEq, Eq, ValueEnum)]
pub enum Action {
    Init,
    Config,
    Doctor,
    Validate,
    Services,
    Images,
    Status,
    Logs,
    LogsFollow,
    Top,
    Shell,
    Deploy,
    Update,
    Pull,
    Build,
    Restart,
    Start,
    Stop,
    Remove,
    Scale,
}
impl Action {
    pub fn name(self) -> &'static str {
        match self {
            Self::Init => "init",
            Self::Config => "config",
            Self::Doctor => "doctor",
            Self::Validate => "validate",
            Self::Services => "services",
            Self::Images => "images",
            Self::Status => "status",
            Self::Logs => "logs",
            Self::LogsFollow => "logs-follow",
            Self::Top => "top",
            Self::Shell => "shell",
            Self::Deploy => "deploy",
            Self::Update => "update",
            Self::Pull => "pull",
            Self::Build => "build",
            Self::Restart => "restart",
            Self::Start => "start",
            Self::Stop => "stop",
            Self::Remove => "remove",
            Self::Scale => "scale",
        }
    }
    pub fn writes(self) -> bool {
        matches!(
            self,
            Self::Deploy
                | Self::Update
                | Self::Pull
                | Self::Build
                | Self::Restart
                | Self::Start
                | Self::Stop
                | Self::Remove
                | Self::Scale
        )
    }
}

pub fn compose_args(repo: &Repo, stack: &str, endpoint: &str) -> Vec<String> {
    let mut args = vec![
        "--host".into(),
        endpoint.into(),
        "compose".into(),
        "--ansi".into(),
        "never".into(),
        "--project-name".into(),
        stack.into(),
        "--project-directory".into(),
        repo.stack_dir(stack).display().to_string(),
    ];
    for file in repo.env_files(stack) {
        args.extend(["--env-file".into(), file.display().to_string()]);
    }
    args.extend([
        "--file".into(),
        repo.stack_dir(stack)
            .join("compose.yaml")
            .display()
            .to_string(),
    ]);
    args
}
fn compose(repo: &Repo, stack: &str, connection: &Connection, args: &[&str]) -> Command {
    compose_at(repo, stack, &connection.endpoint(), args)
}
pub fn compose_at(repo: &Repo, stack: &str, endpoint: &str, args: &[&str]) -> Command {
    let mut command = Command::new("docker");
    command
        .args(compose_args(repo, stack, endpoint))
        .args(args)
        .env_remove("DOCKER_CONTEXT")
        .env_remove("DOCKER_HOST")
        .env_remove("DOCKER_TLS_VERIFY")
        .env_remove("DOCKER_CERT_PATH")
        .env_remove("COMPOSE_FILE")
        .env_remove("COMPOSE_PROJECT_NAME")
        .env_remove("COMPOSE_ENV_FILES")
        .env_remove("COMPOSE_PROFILES")
        .env("COMPOSE_PARALLEL_LIMIT", "1")
        .envs(&repo.environment)
        .current_dir(&repo.root)
        .kill_on_drop(true);
    command
}
async fn capture(command: &mut Command) -> Result<String> {
    let output = tokio::time::timeout(
        Duration::from_secs(30),
        command.stdin(Stdio::null()).output(),
    )
    .await
    .context("Command timed out")??;
    ensure!(
        output.status.success(),
        "Command failed: {}",
        crate::engine::clean(&String::from_utf8_lossy(&output.stderr))
    );
    Ok(String::from_utf8(output.stdout)?)
}
async fn execute(command: &mut Command) -> Result<()> {
    let mut process = command
        .spawn()
        .context("Could not start command; check Docker CLI / Compose installation")?;
    let status = tokio::select! {
        status = process.wait() => status?,
        _ = tokio::signal::ctrl_c() => { process.kill().await?; bail!("Canceled. Remote changes may already have applied; refresh before retrying."); }
    };
    ensure!(status.success(), "Command exited with {status}");
    Ok(())
}
fn confirm(message: &str, yes: bool) -> Result<()> {
    if yes {
        return Ok(());
    }
    ensure!(
        io::stdin().is_terminal(),
        "{message}. Pass --yes to confirm in noninteractive use."
    );
    print!("{message}\nType yes to continue: ");
    io::stdout().flush()?;
    let mut line = String::new();
    io::stdin().read_line(&mut line)?;
    ensure!(line.trim() == "yes", "Canceled");
    Ok(())
}

pub async fn run_cli(repo: &Repo, cli: &Cli, action: Action) -> Result<()> {
    if action == Action::Init {
        return repo.init();
    }
    let (mut target, mut services) = parse_target(repo, cli.target.as_deref(), &cli.services)?;
    let mut fleet_service = None;
    if action == Action::Deploy
        && target.as_ref().is_some_and(|s| !repo.hosts.contains_key(s))
        && services.is_empty()
    {
        fleet_service = target.take();
    }
    if let Some(name) = &target {
        repo.host(name)?;
    }
    if action == Action::Scale {
        ensure!(cli.replicas.is_some(), "scale requires --replicas COUNT");
    }
    if matches!(
        action,
        Action::Shell | Action::Logs | Action::LogsFollow | Action::Scale
    ) {
        ensure!(
            services.len() == 1,
            "{} requires exactly one service",
            action.name()
        );
    }
    if matches!(
        action,
        Action::Start | Action::Stop | Action::Restart | Action::Remove
    ) {
        ensure!(
            !services.is_empty(),
            "{} requires selected services",
            action.name()
        );
    }
    if matches!(
        action,
        Action::Validate | Action::Doctor | Action::Services | Action::Images
    ) {
        ensure!(services.is_empty(), "This action does not accept services");
    }
    if target.is_none() {
        ensure!(
            matches!(
                action,
                Action::Status | Action::Doctor | Action::Validate | Action::Deploy
            ),
            "This action requires a stack"
        );
    }
    if action == Action::Deploy && target.is_none() {
        ensure!(
            services.is_empty(),
            "Use deploy SERVICE for a fleet-wide service rollout"
        );
    }
    ensure!(cli.lines > 0, "--lines must be positive");
    let targets: Vec<_> = target.into_iter().collect();
    let targets = if targets.is_empty() {
        repo.hosts.keys().cloned().collect()
    } else {
        targets
    };
    if action.writes() {
        confirm(
            &format!(
                "{} on {}{}",
                action.name(),
                targets.join(", "),
                fleet_service
                    .as_ref()
                    .map(|s| format!(" / {s}"))
                    .unwrap_or_else(|| format!(
                        " / {}",
                        if services.is_empty() {
                            "all services".into()
                        } else {
                            services.join(", ")
                        }
                    ))
            ),
            cli.yes,
        )?;
    }
    let mut matched = 0;
    for stack in targets {
        let mut scoped = repo.clone();
        if !matches!(
            action,
            Action::Status
                | Action::Logs
                | Action::LogsFollow
                | Action::Top
                | Action::Shell
                | Action::Start
                | Action::Stop
                | Action::Restart
                | Action::Remove
        ) {
            scoped.environment = vault_environment(repo.host(&stack)?).await?;
        }
        let repo = &scoped;
        if action == Action::Config {
            println!("{}", effective_config(repo, &stack, &services).await?);
            matched += 1;
            continue;
        }
        if matches!(action, Action::Validate | Action::Services | Action::Images) {
            repo.check_env(&stack)?;
            let flag = match action {
                Action::Services => "--services",
                Action::Images => "--images",
                _ => "--quiet",
            };
            execute(&mut compose_at(
                repo,
                &stack,
                &repo.host(&stack)?.endpoint,
                &["config", flag],
            ))
            .await?;
            if action == Action::Validate {
                println!("{stack}: configuration checked");
            }
            matched += 1;
            continue;
        }
        let lock_dir = repo.root.join(".cache/tctl");
        let _lock = if action.writes() {
            std::fs::create_dir_all(&lock_dir)?;
            let file = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(lock_dir.join(format!("{stack}.lock")))?;
            file.try_lock()
                .context("Another tctl operation is active for this stack")?;
            Some(file)
        } else {
            None
        };
        let connection = Connection::open(repo.host(&stack)?).await?;
        eprintln!(
            "{}  endpoint={}  project={}  daemon={}",
            stack,
            repo.host(&stack)?.endpoint,
            stack,
            repo.host(&stack)?.expected_name
        );
        if let Some(service) = &fleet_service {
            repo.check_env(&stack)?;
            let configured = capture(&mut compose(
                repo,
                &stack,
                &connection,
                &["config", "--services"],
            ))
            .await?;
            if !configured.lines().any(|s| s == service) {
                println!("{stack}: {service} not configured; skipping");
                continue;
            }
            services = vec![service.clone()];
        }
        matched += 1;
        run_stack(repo, cli, action, &stack, &services, &connection)
            .await
            .with_context(|| {
                format!(
                    "{} failed on {stack}; remaining stacks were not changed",
                    action.name()
                )
            })?;
    }
    if matched == 0 {
        bail!("Service is not configured on any stack");
    }
    Ok(())
}

pub fn parse_target(
    repo: &Repo,
    target: Option<&str>,
    services: &[String],
) -> Result<(Option<String>, Vec<String>)> {
    let mut services = services.to_vec();
    let target = target.map(|s| {
        let (stack, service) = s.split_once('/').map_or((s, None), |(a, b)| (a, Some(b)));
        if let Some(service) = service {
            services.insert(0, service.into());
        }
        repo.normalize(stack).to_string()
    });
    for name in services.iter().chain(target.iter()) {
        safe_name(name)?;
    }
    Ok((target, services))
}

async fn run_stack(
    repo: &Repo,
    cli: &Cli,
    action: Action,
    stack: &str,
    services: &[String],
    connection: &Connection,
) -> Result<()> {
    let refs: Vec<_> = services.iter().map(String::as_str).collect();
    if matches!(
        action,
        Action::Start | Action::Stop | Action::Restart | Action::Remove
    ) {
        let current = snapshot(connection, repo.host(stack)?).await?;
        let selected: Vec<_> = current
            .containers
            .iter()
            .filter(|c| c.project == stack && services.contains(&c.service))
            .collect();
        for service in services {
            ensure!(
                selected.iter().any(|c| &c.service == service),
                "No existing containers for {service}; deploy it first"
            );
        }
        let docker = connection
            .docker
            .clone()
            .with_timeout(Duration::from_secs(40));
        for container in selected {
            connection.verify(repo.host(stack)?).await?;
            match action {
                Action::Start => {
                    docker
                        .start_container(&container.id, None::<StartContainerOptions>)
                        .await?
                }
                Action::Stop => {
                    docker
                        .stop_container(
                            &container.id,
                            Some(StopContainerOptionsBuilder::default().t(10).build()),
                        )
                        .await?
                }
                Action::Restart => {
                    docker
                        .restart_container(
                            &container.id,
                            Some(RestartContainerOptionsBuilder::default().t(10).build()),
                        )
                        .await?
                }
                Action::Remove => {
                    if container.state == "running" {
                        docker
                            .stop_container(
                                &container.id,
                                Some(StopContainerOptionsBuilder::default().t(10).build()),
                            )
                            .await?;
                    }
                    docker
                        .remove_container(
                            &container.id,
                            Some(
                                RemoveContainerOptionsBuilder::default()
                                    .v(false)
                                    .force(false)
                                    .build(),
                            ),
                        )
                        .await?;
                }
                _ => unreachable!(),
            }
            println!("{}: {} {}", stack, action.name(), container.name);
        }
        return Ok(());
    }
    match action {
        Action::Status => {
            let snapshot = snapshot(connection, repo.host(stack)?).await?;
            println!("NAME\tSERVICE\tSTATE\tSTATUS\tIMAGE");
            for c in snapshot.containers.iter().filter(|c| {
                c.project == stack && (services.is_empty() || services.contains(&c.service))
            }) {
                println!(
                    "{}\t{}\t{}\t{}\t{}",
                    c.name, c.service, c.state, c.status, c.image
                );
            }
            return Ok(());
        }
        Action::Logs | Action::LogsFollow | Action::Top | Action::Shell => {
            let containers = connection
                .docker
                .list_containers(Some(
                    ListContainersOptionsBuilder::default().all(true).build(),
                ))
                .await?;
            let selected: Vec<_> = containers
                .iter()
                .filter(|c| {
                    c.labels.as_ref().is_some_and(|l| {
                        l.get("com.docker.compose.project")
                            .is_some_and(|s| s == stack)
                            && (services.is_empty()
                                || l.get("com.docker.compose.service")
                                    .is_some_and(|s| services.contains(s)))
                    })
                })
                .collect();
            ensure!(!selected.is_empty(), "No matching containers");
            if action == Action::Shell {
                ensure!(
                    selected.len() == 1,
                    "Select a service with exactly one container for shell access"
                );
            }
            if matches!(action, Action::Logs | Action::LogsFollow) {
                let streams: Vec<_> = selected
                    .iter()
                    .map(|c| {
                        connection.docker.logs(
                            c.id.as_deref().unwrap_or(""),
                            Some(
                                LogsOptionsBuilder::default()
                                    .stdout(true)
                                    .stderr(true)
                                    .timestamps(true)
                                    .tail(&cli.lines.to_string())
                                    .follow(cli.follow || action == Action::LogsFollow)
                                    .build(),
                            ),
                        )
                    })
                    .collect();
                let mut logs = futures_util::stream::select_all(streams);
                while let Some(line) = logs.try_next().await? {
                    print!("{}", crate::engine::clean(&line.to_string()));
                }
            } else {
                for container in selected {
                    let id = container.id.as_deref().context("Missing container id")?;
                    let mut cmd = Command::new("docker");
                    cmd.args(["--host", &connection.endpoint()])
                        .env_remove("DOCKER_CONTEXT")
                        .kill_on_drop(true);
                    if action == Action::Shell {
                        cmd.args(["exec", "-it", id, "sh"]);
                    } else {
                        cmd.args(["top", id]);
                    }
                    execute(&mut cmd).await?;
                }
            }
            return Ok(());
        }
        _ => {}
    }
    repo.check_env(stack)?;
    execute(&mut compose(
        repo,
        stack,
        connection,
        &["config", "--quiet"],
    ))
    .await?;
    if action == Action::Doctor {
        println!("{stack}: identity and Compose configuration valid");
        return check_mounts(repo, stack, connection).await;
    }
    if action == Action::Validate {
        println!("{stack}: Compose configuration valid");
        return Ok(());
    }
    if action == Action::Services || action == Action::Images {
        return execute(&mut compose(
            repo,
            stack,
            connection,
            &[
                "config",
                if action == Action::Services {
                    "--services"
                } else {
                    "--images"
                },
            ],
        ))
        .await;
    }
    let configured = capture(&mut compose(
        repo,
        stack,
        connection,
        &["config", "--services"],
    ))
    .await?;
    for service in services {
        ensure!(
            configured.lines().any(|s| s == service),
            "Unknown service {service} in {stack}"
        );
    }
    connection.verify(repo.host(stack)?).await?;
    if action == Action::Scale {
        let model: serde_json::Value = serde_json::from_str(
            &capture(&mut compose(
                repo,
                stack,
                connection,
                &["config", "--format", "json"],
            ))
            .await?,
        )?;
        let service = &model["services"][&services[0]];
        if cli.replicas.unwrap_or(0) > 1 {
            ensure!(
                service["container_name"].is_null()
                    && !service["ports"]
                        .as_array()
                        .is_some_and(|ports| ports.iter().any(|p| !p["published"].is_null())),
                "Cannot scale a service with fixed container_name or published ports"
            );
        }
    }
    let changed = if matches!(action, Action::Deploy | Action::Update) {
        reconcile(repo, stack, services, connection, &configured).await?
    } else {
        false
    };
    if matches!(action, Action::Pull | Action::Update) {
        let mut args = vec!["pull", "--ignore-buildable"];
        args.extend(&refs);
        execute(&mut compose(repo, stack, connection, &args)).await?;
    }
    if matches!(action, Action::Pull | Action::Build | Action::Update) {
        let mut args = vec!["build", "--pull"];
        args.extend(&refs);
        execute(&mut compose(repo, stack, connection, &args)).await?;
        if action != Action::Update {
            return Ok(());
        }
    }
    let scale = format!(
        "{}={}",
        services.first().map_or("", String::as_str),
        cli.replicas.unwrap_or(0)
    );
    let mut args = match action {
        Action::Deploy | Action::Update | Action::Scale => {
            let mut a = vec!["up", "--detach", "--wait", "--wait-timeout", "120"];
            if action == Action::Deploy {
                a.push("--no-build");
            }
            if !services.is_empty() {
                a.push("--no-deps");
            }
            if cli.force || changed {
                a.push("--force-recreate");
            }
            if action == Action::Scale {
                a.extend(["--scale", scale.as_str()]);
            }
            a
        }
        Action::Remove => vec!["rm", "--stop", "--force"],
        Action::Start => vec!["start"],
        Action::Stop => vec!["stop"],
        Action::Restart => vec!["restart"],
        _ => bail!("Unsupported action"),
    };
    args.extend(&refs);
    execute(&mut compose(repo, stack, connection, &args)).await?;
    execute(&mut compose(repo, stack, connection, &["ps", "--all"])).await
}

async fn reconcile(
    repo: &Repo,
    stack: &str,
    services: &[String],
    connection: &Connection,
    configured: &str,
) -> Result<bool> {
    let selected: Vec<_> = if services.is_empty() {
        configured.lines().map(str::to_string).collect()
    } else {
        services.to_vec()
    };
    let environment = capture(&mut compose(
        repo,
        stack,
        connection,
        &["config", "--environment"],
    ))
    .await?;
    let root = environment
        .lines()
        .find_map(|s| s.strip_prefix("STACK_DATA_ROOT="))
        .context("STACK_DATA_ROOT is required")?;
    safe_path(root)?;
    let mut changed = false;
    for service in selected {
        safe_name(&service)?;
        let dir = repo
            .root
            .join("docker/services")
            .join(&service)
            .join("files");
        if !dir.is_dir() {
            continue;
        }
        let mut files = Vec::new();
        collect_files(&dir, &mut files)?;
        for file in files {
            let relative = file.strip_prefix(&dir)?.to_string_lossy();
            let destination = format!("{root}/{service}/{relative}");
            safe_path(&destination)?;
            let parent = Path::new(&destination)
                .parent()
                .context("Invalid destination")?
                .display();
            let host = repo.host(stack)?;
            let script = format!(
                "set -eu; install -d -m 0755 '{parent}'; t=$(mktemp '{destination}.tctl.XXXXXX'); trap 'rm -f \"$t\"' EXIT; cat > \"$t\"; chmod 0644 \"$t\"; if test -f '{destination}' && cmp -s \"$t\" '{destination}'; then exit 0; fi; mv -f \"$t\" '{destination}'; exit 10"
            );
            let mut cmd = if let Some(alias) = host.ssh() {
                let mut c = Command::new("ssh");
                c.args([
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    "ConnectTimeout=8",
                    alias,
                    &script,
                ]);
                c
            } else {
                let mut c = Command::new("sh");
                c.args(["-c", &script]);
                c
            };
            let mut child = cmd.stdin(Stdio::piped()).kill_on_drop(true).spawn()?;
            let mut input = child.stdin.take().context("No child input")?;
            let data = tokio::fs::read(&file).await?;
            let transfer = async {
                input.write_all(&data).await?;
                drop(input);
                child.wait().await
            };
            let status = tokio::time::timeout(Duration::from_secs(30), transfer).await??;
            match status.code() {
                Some(0) => {}
                Some(10) => {
                    changed = true;
                    println!("{stack}: updated {service}/{relative}");
                }
                _ => bail!("Managed file transfer failed for {service}/{relative}"),
            }
        }
    }
    Ok(changed)
}
fn collect_files(dir: &Path, files: &mut Vec<std::path::PathBuf>) -> Result<()> {
    ensure!(
        !std::fs::symlink_metadata(dir)?.is_symlink(),
        "Managed file directories must not be symlinks"
    );
    for entry in std::fs::read_dir(dir)? {
        let entry = entry?;
        let kind = entry.file_type()?;
        ensure!(!kind.is_symlink(), "Managed files must not be symlinks");
        if kind.is_dir() {
            collect_files(&entry.path(), files)?;
        } else if kind.is_file() {
            files.push(entry.path());
        }
    }
    files.sort();
    Ok(())
}
async fn check_mounts(repo: &Repo, stack: &str, connection: &Connection) -> Result<()> {
    let model: serde_json::Value = serde_json::from_str(
        &capture(&mut compose(
            repo,
            stack,
            connection,
            &["config", "--format", "json"],
        ))
        .await?,
    )?;
    if let Some(networks) = model["networks"].as_object() {
        for (key, network) in networks {
            if network["external"].as_bool() == Some(true) {
                let name = network["name"].as_str().unwrap_or(key);
                connection
                    .docker
                    .inspect_network(name, None::<InspectNetworkOptions>)
                    .await
                    .with_context(|| format!("Missing external network {name}"))?;
            }
        }
    }
    println!(
        "{stack}: external networks verified. Bind mounts and application data must be provisioned on the host."
    );
    Ok(())
}

pub async fn vault_environment(
    host: &crate::config::Host,
) -> Result<std::collections::BTreeMap<String, String>> {
    let mut environment = std::collections::BTreeMap::new();
    for item in &host.vault_items {
        ensure!(
            !item.is_empty() && item.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-'),
            "Use immutable vault item IDs"
        );
        let mut command = Command::new("bw");
        command.args(["get", "item", item]).kill_on_drop(true);
        let output = tokio::time::timeout(
            Duration::from_secs(15),
            command.stdin(Stdio::null()).output(),
        )
        .await
        .context("Vault request timed out; unlock with BW_SESSION")??;
        ensure!(
            output.status.success(),
            "Cannot read configured vault item. Log in, unlock BW_SESSION, and check item access."
        );
        let value: serde_json::Value =
            serde_json::from_slice(&output.stdout).context("Vault returned invalid JSON")?;
        environment.extend(hidden_fields(&value)?);
    }
    Ok(environment)
}
fn hidden_fields(item: &serde_json::Value) -> Result<std::collections::BTreeMap<String, String>> {
    let mut fields = std::collections::BTreeMap::new();
    for field in item["fields"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|f| f["type"].as_u64() == Some(1))
    {
        let name = field["name"].as_str().context("Vault field needs a name")?;
        ensure!(
            name.as_bytes()
                .first()
                .is_some_and(|b| b.is_ascii_alphabetic() || *b == b'_')
                && name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_'),
            "Invalid environment variable name in vault"
        );
        ensure!(
            ![
                "PATH",
                "HOME",
                "SHELL",
                "BASH_ENV",
                "ENV",
                "LD_PRELOAD",
                "DYLD_INSERT_LIBRARIES"
            ]
            .contains(&name)
                && !name.starts_with("DOCKER_")
                && !name.starts_with("COMPOSE_"),
            "Vault fields cannot override process or Docker routing variables"
        );
        let value = field["value"]
            .as_str()
            .context("Vault field needs a string value")?;
        ensure!(!fields.contains_key(name), "Duplicate vault field name");
        fields.insert(name.into(), value.into());
    }
    ensure!(
        !fields.is_empty(),
        "Vault item has no hidden environment fields"
    );
    Ok(fields)
}

pub async fn effective_config(repo: &Repo, stack: &str, selected: &[String]) -> Result<String> {
    repo.check_env(stack)?;
    let raw = capture(&mut compose_at(
        repo,
        stack,
        &repo.host(stack)?.endpoint,
        &["config", "--format", "json"],
    ))
    .await?;
    let model: serde_json::Value = serde_json::from_str(&raw)?;
    let services = model["services"]
        .as_object()
        .context("Compose model has no services")?;
    for name in selected {
        ensure!(services.contains_key(name), "Unknown service {name}");
    }
    let mut safe = serde_json::Map::new();
    for (name, service) in services
        .iter()
        .filter(|(name, _)| selected.is_empty() || selected.contains(name))
    {
        let mut fields = serde_json::Map::new();
        for key in [
            "image",
            "container_name",
            "ports",
            "volumes",
            "networks",
            "depends_on",
            "profiles",
            "restart",
        ] {
            if !service[key].is_null() {
                fields.insert(key.into(), service[key].clone());
            }
        }
        safe.insert(name.clone(), fields.into());
    }
    Ok(serde_json::to_string_pretty(
        &serde_json::json!({"source":repo.stack_dir(stack).join("compose.yaml"),"services":safe}),
    )?)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn explicit_compose_target_and_env_order() {
        let repo = Repo::load(Some(
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .parent()
                .unwrap(),
        ))
        .unwrap();
        let args = compose_args(&repo, "nexus", "unix:///tmp/tctl.sock");
        assert_eq!(&args[..3], &["--host", "unix:///tmp/tctl.sock", "compose"]);
        let envs: Vec<_> = args
            .windows(2)
            .filter(|p| p[0] == "--env-file")
            .map(|p| p[1].clone())
            .collect();
        assert!(envs[0].ends_with("docker/.env"));
        assert!(envs[1].ends_with("docker/stacks/nexus/.env"));
        let (target, services) = parse_target(&repo, Some("nx/grafana"), &[]).unwrap();
        assert_eq!(target.as_deref(), Some("nexus"));
        assert_eq!(services, ["grafana"]);
        assert!(parse_target(&repo, Some("nx/--help"), &[]).is_err());
    }
    #[test]
    fn vault_fields_are_scoped_and_cannot_override_routing() {
        let value = serde_json::json!({"fields":[{"type":1,"name":"APP_TOKEN","value":"hidden"},{"type":0,"name":"note","value":"not-exported"}]});
        let fields = hidden_fields(&value).unwrap();
        assert_eq!(fields.len(), 1);
        assert_eq!(fields["APP_TOKEN"], "hidden");
        for name in ["PATH", "DOCKER_HOST", "COMPOSE_FILE", "INVALID-NAME"] {
            assert!(
                hidden_fields(&serde_json::json!({"fields":[{"type":1,"name":name,"value":"x"}]}))
                    .is_err()
            );
        }
        assert!(hidden_fields(&serde_json::json!({"fields":[{"type":1,"name":"TOKEN","value":"a"},{"type":1,"name":"TOKEN","value":"b"}]})).is_err());
    }
}
