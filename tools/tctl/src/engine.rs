use crate::config::{Host, Repo};
use anyhow::{Context, Result, bail, ensure};
use bollard::{API_DEFAULT_VERSION, Docker, query_parameters::*};
use futures_util::{StreamExt, TryStreamExt};
use serde_json::Value;
use std::{
    process::Stdio,
    time::{Duration, Instant},
};
use tokio::{
    process::{Child, Command},
    sync::{mpsc, watch},
    task::JoinHandle,
};

pub struct Connection {
    pub docker: Docker,
    socket: String,
    _ssh: Option<Child>,
    _directory: Option<tempfile::TempDir>,
}
impl Connection {
    pub fn endpoint(&self) -> String {
        format!("unix://{}", self.socket)
    }
    pub async fn open(host: &Host) -> Result<Self> {
        host.validate()?;
        let mut child = None;
        let mut directory = None;
        let socket = if let Some(alias) = host.ssh() {
            let dir = tempfile::Builder::new()
                .prefix("tctl-")
                .tempdir_in("/tmp")?;
            let socket = dir.path().join("docker.sock");
            let error_path = dir.path().join("ssh-error");
            let error_file = std::fs::File::create(&error_path)?;
            let mut ssh = Command::new("ssh");
            ssh.args([
                "-nNT",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "ConnectTimeout=8",
                "-o",
                "ServerAliveInterval=5",
                "-o",
                "ServerAliveCountMax=2",
                "-L",
            ])
            .arg(format!("{}:{}", socket.display(), host.socket))
            .arg(alias)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(error_file)
            .kill_on_drop(true);
            let mut process = ssh
                .spawn()
                .context("Install OpenSSH to connect to remote Docker")?;
            let deadline = Instant::now() + Duration::from_secs(10);
            while !socket.exists() {
                if let Some(status) = process.try_wait()? {
                    bail!(
                        "SSH {status}: {}",
                        std::fs::read_to_string(error_path)
                            .unwrap_or_default()
                            .trim()
                    );
                }
                ensure!(
                    Instant::now() < deadline,
                    "SSH tunnel timed out; check alias, credentials and trusted host key"
                );
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            child = Some(process);
            directory = Some(dir);
            socket.to_string_lossy().into_owned()
        } else {
            host.endpoint.trim_start_matches("unix://").to_string()
        };
        let docker = Docker::connect_with_unix(&socket, 10, API_DEFAULT_VERSION)?;
        let docker =
            tokio::time::timeout(Duration::from_secs(10), docker.negotiate_version()).await??;
        let connection = Self {
            docker,
            socket,
            _ssh: child,
            _directory: directory,
        };
        connection.verify(host).await?;
        Ok(connection)
    }
    pub async fn verify(&self, host: &Host) -> Result<()> {
        let info = self.docker.info().await?;
        ensure!(
            info.name.as_deref() == Some(&host.expected_name),
            "Wrong daemon: expected {}, received {}. Refusing target.",
            host.expected_name,
            info.name.unwrap_or_default()
        );
        Ok(())
    }
}

#[derive(Clone, Default, Debug)]
pub struct Container {
    pub id: String,
    pub name: String,
    pub service: String,
    pub project: String,
    pub image: String,
    pub state: String,
    pub status: String,
    pub ports: Vec<Port>,
}
#[derive(Clone, Debug)]
pub struct Port {
    pub private: u16,
    pub public: Option<u16>,
    pub ip: String,
    pub protocol: String,
}
impl Container {
    pub fn from_json(v: &Value) -> Self {
        let ports = v["Ports"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|p| Port {
                private: p["PrivatePort"].as_u64().unwrap_or(0) as u16,
                public: p["PublicPort"].as_u64().map(|p| p as u16),
                ip: string(p, "IP"),
                protocol: string(p, "Type"),
            })
            .collect();
        Self {
            id: string(v, "Id"),
            name: v["Names"][0]
                .as_str()
                .unwrap_or("")
                .trim_start_matches('/')
                .into(),
            service: v["Labels"]["com.docker.compose.service"]
                .as_str()
                .unwrap_or("")
                .into(),
            project: v["Labels"]["com.docker.compose.project"]
                .as_str()
                .unwrap_or("")
                .into(),
            image: string(v, "Image"),
            state: string(v, "State"),
            status: string(v, "Status"),
            ports,
        }
    }
}
pub fn string(v: &Value, key: &str) -> String {
    v[key].as_str().unwrap_or("").to_string()
}

#[derive(Clone, Default)]
pub struct Snapshot {
    pub containers: Vec<Container>,
    pub images: Vec<Value>,
    pub networks: Vec<Value>,
    pub volumes: Vec<Value>,
}
pub async fn snapshot(connection: &Connection, host: &Host) -> Result<Snapshot> {
    connection.verify(host).await?;
    let docker = &connection.docker;
    let (containers, images, networks, volumes) = tokio::try_join!(
        docker.list_containers(Some(
            ListContainersOptionsBuilder::default().all(true).build()
        )),
        docker.list_images(None::<ListImagesOptions>),
        docker.list_networks(None::<ListNetworksOptions>),
        docker.list_volumes(None::<ListVolumesOptions>),
    )?;
    let mut containers: Vec<_> = containers
        .into_iter()
        .map(|c| Container::from_json(&serde_json::to_value(c).unwrap_or_default()))
        .collect();
    containers
        .sort_by(|a, b| (&a.project, &a.service, &a.name).cmp(&(&b.project, &b.service, &b.name)));
    Ok(Snapshot {
        containers,
        images: images
            .into_iter()
            .map(|v| serde_json::to_value(v).unwrap_or_default())
            .collect(),
        networks: networks
            .into_iter()
            .map(|v| serde_json::to_value(v).unwrap_or_default())
            .collect(),
        volumes: volumes
            .volumes
            .unwrap_or_default()
            .into_iter()
            .map(|v| serde_json::to_value(v).unwrap_or_default())
            .collect(),
    })
}

#[derive(Clone, Default, Debug)]
pub struct Stats {
    pub cpu: f64,
    pub memory: u64,
    pub limit: u64,
    pub rx: u64,
    pub tx: u64,
}
impl Stats {
    pub fn from_json(v: &Value) -> Self {
        let cpu = v["cpu_stats"]["cpu_usage"]["total_usage"]
            .as_u64()
            .unwrap_or(0)
            .saturating_sub(
                v["precpu_stats"]["cpu_usage"]["total_usage"]
                    .as_u64()
                    .unwrap_or(0),
            );
        let system = v["cpu_stats"]["system_cpu_usage"]
            .as_u64()
            .unwrap_or(0)
            .saturating_sub(v["precpu_stats"]["system_cpu_usage"].as_u64().unwrap_or(0));
        let cores = v["cpu_stats"]["online_cpus"].as_u64().unwrap_or_else(|| {
            v["cpu_stats"]["cpu_usage"]["percpu_usage"]
                .as_array()
                .map_or(1, |v| v.len() as u64)
        });
        let usage = v["memory_stats"]["usage"].as_u64().unwrap_or(0);
        let cache = v["memory_stats"]["stats"]["inactive_file"]
            .as_u64()
            .or_else(|| v["memory_stats"]["stats"]["total_inactive_file"].as_u64())
            .unwrap_or(0);
        let (mut rx, mut tx) = (0, 0);
        if let Some(nets) = v["networks"].as_object() {
            for net in nets.values() {
                rx += net["rx_bytes"].as_u64().unwrap_or(0);
                tx += net["tx_bytes"].as_u64().unwrap_or(0);
            }
        }
        Self {
            cpu: if system > 0 {
                cpu as f64 / system as f64 * cores as f64 * 100.0
            } else {
                0.0
            },
            memory: usage.saturating_sub(cache),
            limit: v["memory_stats"]["limit"].as_u64().unwrap_or(0),
            rx,
            tx,
        }
    }
}

pub enum Update {
    Snapshot(String, Snapshot),
    Error(String, String),
    Event(String, String),
    Stats(String, String, Stats),
    Log(u64, String),
    Detail(u64, String),
    Done(String),
    Desired(String, std::result::Result<Vec<String>, String>),
    Tunnel(u16, Child),
}

pub fn watch_host(
    name: String,
    host: Host,
    tx: mpsc::Sender<Update>,
    active: watch::Receiver<Vec<String>>,
    mut refresh: watch::Receiver<u64>,
) -> JoinHandle<()> {
    tokio::spawn(async move {
        let mut delay = 1;
        loop {
            match connected(&name, &host, &tx, &active, &mut refresh).await {
                Ok(()) => return,
                Err(error) => {
                    if tx
                        .send(Update::Error(name.clone(), format!("{error:#}")))
                        .await
                        .is_err()
                    {
                        return;
                    }
                }
            }
            tokio::select! { _ = tx.closed() => return, _ = tokio::time::sleep(Duration::from_secs(delay)) => {}, _ = refresh.changed() => {} }
            delay = (delay * 2).min(30);
        }
    })
}
async fn connected(
    name: &str,
    host: &Host,
    tx: &mpsc::Sender<Update>,
    active: &watch::Receiver<Vec<String>>,
    refresh: &mut watch::Receiver<u64>,
) -> Result<()> {
    let connection = Connection::open(host).await?;
    let mut events = connection.docker.events(None::<EventsOptions>);
    // Poll the event stream before the first snapshot so changes during the fetch are reconciled.
    let (event_tx, mut event_rx) = mpsc::channel(64);
    let event_task = tokio::spawn(async move {
        while let Some(event) = events.next().await {
            if event_tx.send(event).await.is_err() {
                break;
            }
        }
    });
    let guard = Abort(event_task);
    let mut refresh_tick = tokio::time::interval(Duration::from_secs(15));
    refresh_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut stats_tick = tokio::time::interval(Duration::from_secs(3));
    stats_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut current = Snapshot::default();
    let mut dirty = false;
    let mut debounce = tokio::time::interval(Duration::from_millis(250));
    let mut stats_tasks: std::collections::HashMap<String, Abort> =
        std::collections::HashMap::new();
    loop {
        let refresh_now = tokio::select! {
            _ = tx.closed() => { drop(guard); return Ok(()); }
            _ = refresh_tick.tick() => true,
            _ = refresh.changed() => true,
            event = event_rx.recv() => {
                let event = event.context("Docker event stream disconnected")??;
                let timestamp = event.time.and_then(|t| time::OffsetDateTime::from_unix_timestamp(t).ok())
                    .map_or("--:--:--".into(), |t| format!("{:02}:{:02}:{:02}Z", t.hour(), t.minute(), t.second()));
                let text = format!("{timestamp} {} {}", event.action.unwrap_or_default(), event.actor.and_then(|a| a.attributes).and_then(|a| a.get("name").cloned()).unwrap_or_default());
                let _ = tx.try_send(Update::Event(name.into(), text));
                dirty = true;
                false
            }
            _ = debounce.tick(), if dirty => { dirty = false; true }
            _ = stats_tick.tick() => {
                if !active.borrow().is_empty() && !active.borrow().iter().any(|h| h == name) { stats_tasks.clear(); }
                else {
                    stats_tasks.retain(|id,task| !task.0.is_finished() && current.containers.iter().any(|c|&c.id == id && c.state == "running"));
                    for container in current.containers.iter().filter(|c|c.state == "running") {
                        let id = container.id.clone();
                        stats_tasks.entry(id.clone()).or_insert_with(|| {
                            let docker = connection.docker.clone(); let tx = tx.clone(); let name = name.to_string();
                            Abort(tokio::spawn(async move {
                                let mut stream = docker.stats(&id, Some(StatsOptionsBuilder::default().stream(true).build()));
                                while let Some(Ok(stats)) = stream.next().await {
                                    let stats = Stats::from_json(&serde_json::to_value(stats).unwrap_or_default());
                                    if tx.send(Update::Stats(name.clone(), id.clone(), stats)).await.is_err() { break; }
                                }
                            }))
                        });
                    }
                }
                false
            }

        };
        if refresh_now {
            current = tokio::time::timeout(Duration::from_secs(10), snapshot(&connection, host))
                .await??;
            tx.send(Update::Snapshot(name.into(), current.clone()))
                .await?;
        }
    }
}
pub struct Abort(pub JoinHandle<()>);
impl Drop for Abort {
    fn drop(&mut self) {
        self.0.abort();
    }
}

pub fn detail(
    repo: Repo,
    host_name: String,
    id: String,
    generation: u64,
    logs: bool,
    tx: mpsc::Sender<Update>,
) -> Abort {
    Abort(tokio::spawn(async move {
        let mut tail = "200";
        let mut since = 0i32;
        let mut delay = 1;
        loop {
            let result: Result<()> = async {
                let connection = Connection::open(repo.host(&host_name)?).await?;
                if logs {
                    let mut stream = connection.docker.logs(&id, Some(LogsOptionsBuilder::default()
                        .stdout(true).stderr(true).timestamps(true).follow(true).tail(tail).since(since).build()));
                    while let Some(line) = stream.try_next().await? {
                        tx.send(Update::Log(generation, clean(&line.to_string()))).await?;
                        // Reconnect with a one-second overlap; label the boundary rather than claim exact-once logs.
                        if let Some(timestamp) = line.to_string().split_whitespace().next()
                            && let Ok(timestamp) = time::OffsetDateTime::parse(timestamp, &time::format_description::well_known::Rfc3339) {
                            since = timestamp.unix_timestamp().saturating_sub(1).try_into().unwrap_or(0);
                        }
                        tail = "all";
                        delay = 1;
                    }
                    anyhow::bail!("Log stream ended");
                } else {
                    let inspect = connection.docker.inspect_container(&id, None::<InspectContainerOptions>).await?;
                    let raw = serde_json::to_value(inspect)?;
                    // Allowlist diagnostics; Env, labels and command args can contain secrets.
                    let safe = serde_json::json!({"Id": raw["Id"], "Name": raw["Name"], "State": raw["State"], "RestartCount": raw["RestartCount"], "Mounts": raw["Mounts"], "Ports": raw["NetworkSettings"]["Ports"], "Networks": raw["NetworkSettings"]["Networks"], "Image": raw["Image"]});
                    tx.send(Update::Detail(generation, clean(&serde_json::to_string_pretty(&safe)?))).await?;
                }
                Ok(())
            }.await;
            if !logs {
                if let Err(e) = result {
                    let _ = tx.send(Update::Detail(generation, format!("{e:#}"))).await;
                }
                return;
            }
            let message = format!(
                "--- {}. Reconnecting in {delay}s; boundary may contain duplicates or a gap ---",
                result.err().map(|e| format!("{e:#}")).unwrap_or_default()
            );
            if tx.send(Update::Log(generation, message)).await.is_err() {
                return;
            }
            tokio::select! { _ = tx.closed() => return, _ = tokio::time::sleep(Duration::from_secs(delay)) => {} }
            delay = (delay * 2).min(30);
        }
    }))
}
pub fn clean(text: &str) -> String {
    text.chars()
        .filter(|c| !c.is_control() || *c == '\n' || *c == '\t')
        .take(65536)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn linux_stats_and_counter_reset() {
        let v = serde_json::json!({"cpu_stats":{"cpu_usage":{"total_usage":150},"system_cpu_usage":1100,"online_cpus":4},"precpu_stats":{"cpu_usage":{"total_usage":100},"system_cpu_usage":1000},"memory_stats":{"usage":1000,"stats":{"inactive_file":200}},"networks":{"eth0":{"rx_bytes":42,"tx_bytes":12}}});
        let s = Stats::from_json(&v);
        assert_eq!((s.cpu, s.memory, s.rx, s.tx), (200.0, 800, 42, 12));
        let mut reset = v;
        reset["cpu_stats"]["system_cpu_usage"] = 0.into();
        assert_eq!(Stats::from_json(&reset).cpu, 0.0);
    }
    #[test]
    fn decode_compose_identity_and_ports() {
        let c = Container::from_json(
            &serde_json::json!({"Id":"abc", "Names":["/web-1"], "Labels":{"com.docker.compose.project":"nexus","com.docker.compose.service":"web"},"Ports":[{"PrivatePort":80,"PublicPort":8080,"Type":"tcp","IP":"0.0.0.0"}]}),
        );
        assert_eq!(
            (c.name.as_str(), c.project.as_str(), c.service.as_str()),
            ("web-1", "nexus", "web")
        );
        assert_eq!(c.ports[0].public, Some(8080));
        assert_eq!(clean("hello\x1b]52;bad\x07\n"), "hello]52;bad\n");
    }
}
