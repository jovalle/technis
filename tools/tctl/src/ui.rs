use crate::{
    config::Repo,
    engine::{self, Abort, Container, Port, Snapshot, Stats, Update},
};
use anyhow::{Context, Result, ensure};
use crossterm::event::{Event, EventStream, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use futures_util::StreamExt;
use ratatui::{prelude::*, widgets::*};
use std::{
    collections::{BTreeMap, HashMap, VecDeque},
    io::IsTerminal,
    process::Stdio,
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    process::{Child, Command},
    sync::{mpsc, oneshot, watch},
};

const TABS: [&str; 8] = [
    "Stacks",
    "Services",
    "Containers",
    "Images",
    "Volumes",
    "Networks",
    "Ports",
    "Events",
];
const ACCENT: Color = Color::Cyan;
const MUTED: Color = Color::DarkGray;
const HELP: &str = "NAVIGATION\n  j/k arrows     move / scroll\n  g/G Home/End   first / last\n  Ctrl-u/d       half page\n  h/l Tab        previous / next tab\n  1..8           jump to resource tab\n  [ / ]          previous / next scope (includes all)\n  b / Ctrl-b     searchable stack scope\n  p              toggle selected-row details\n  o / O          cycle sort / reverse order\n  /              filter: text stack:nexus state:unhealthy (logs: substring)\n  :              command mode\n  Enter          stack -> services -> containers -> logs\n  Esc            back / clear filter / close\n\nINSPECT & MANAGE\n  L              follow selected container logs\n  i / c          inspect / effective configuration\n  t              containers with live CPU/memory/network\n  e              shell in selected service\n  a              action menu\n  r / s / S      restart / stop / start\n  D / U          deploy / update\n  x              remove service containers\n  R              refresh all hosts\n  f              forward selected port / toggle log follow\n  ?              this help\n  q / Ctrl-c     quit; close owned tunnels\n\nCOMMANDS\n  :stacks :services :containers :images :volumes :networks :ports :events\n  :ctx all|nexus|nexus,stargate\n  :sort name|stack|state [desc]\n  :deploy   :update   :validate   :doctor\n  :scale 2     :forward 8080     :unforward 8080\n  :tunnels     :refresh          :q\n  :cancel [STACK] stop local operation; does not roll back\n\nEdit [keys] in tctl.toml to override bindings.\nLogs and health-check output may contain application secrets.";

#[derive(Default)]
struct HostState {
    snapshot: Snapshot,
    seen: Option<Instant>,
    error: Option<String>,
    stats: HashMap<String, (Stats, Instant)>,
    events: VecDeque<(u64, String)>,
    desired: Vec<String>,
    config_error: Option<String>,
}
impl HostState {
    fn live(&self) -> bool {
        self.error.is_none()
            && self
                .seen
                .is_some_and(|s| s.elapsed() < Duration::from_secs(30))
    }
    fn label(&self) -> &'static str {
        if self.error.is_some() {
            "OFFLINE"
        } else if self.seen.is_none() {
            "CONNECTING"
        } else if !self.live() {
            "STALE"
        } else {
            "LIVE"
        }
    }
}
#[derive(Clone)]
struct RowData {
    key: String,
    host: String,
    cells: Vec<String>,
    status: String,
    service: Option<String>,
    container: Option<Container>,
    port: Option<Port>,
    event_sequence: u64,
}
#[derive(Clone)]
struct Pending {
    host: String,
    action: String,
    services: Vec<String>,
    extra: Vec<String>,
    token: String,
}
#[derive(Clone)]
struct Navigation {
    scope: Vec<String>,
    tab: usize,
    filter: String,
    service: Option<(String, String)>,
    selected: usize,
    selected_key: Option<String>,
    sort: usize,
    descending: bool,
}
struct App {
    repo: Repo,
    hosts: Vec<String>,
    scope: Vec<String>,
    history: Vec<Navigation>,
    service: Option<(String, String)>,
    sort: usize,
    descending: bool,
    picker_index: usize,
    event_sequence: u64,
    states: BTreeMap<String, HostState>,
    tab: usize,
    selected: usize,
    selected_key: Option<String>,
    preview: bool,
    detail_row: Option<RowData>,
    detail_filter: String,
    operation_host: Option<String>,
    filter: String,
    input: Option<char>,
    buffer: String,
    help: bool,
    menu: Option<usize>,
    pending: Option<Pending>,
    detail: Option<String>,
    logs: bool,
    lines: VecDeque<String>,
    log_bytes: usize,
    dropped: usize,
    scroll: usize,
    viewport_height: usize,
    follow: bool,
    generation: u64,
    detail_task: Option<Abort>,
    detail_id: Option<String>,
    detail_service: Option<String>,
    notice: String,
    busy: HashMap<String, Abort>,
    cancellations: HashMap<String, oneshot::Sender<()>>,
    tunnels: HashMap<u16, Child>,
}
impl App {
    fn new(repo: Repo) -> Self {
        let hosts: Vec<_> = repo.hosts.keys().cloned().collect();
        let states = hosts
            .iter()
            .map(|h| (h.clone(), HostState::default()))
            .collect();
        Self {
            repo,
            hosts,
            states,
            scope: Vec::new(),
            history: Vec::new(),
            service: None,
            sort: 2,
            descending: false,
            picker_index: 0,
            event_sequence: 0,
            tab: 1,
            selected: 0,
            selected_key: None,
            preview: true,
            detail_row: None,
            detail_filter: String::new(),
            operation_host: None,
            filter: String::new(),
            input: None,
            buffer: String::new(),
            help: false,
            menu: None,
            pending: None,
            detail: None,
            logs: false,
            lines: VecDeque::new(),
            log_bytes: 0,
            dropped: 0,
            scroll: 0,
            viewport_height: 16,
            follow: true,
            generation: 0,
            detail_task: None,
            detail_id: None,
            detail_service: None,
            notice: String::new(),
            busy: HashMap::new(),
            cancellations: HashMap::new(),
            tunnels: HashMap::new(),
        }
    }
    fn scoped_hosts(&self) -> &[String] {
        if self.scope.is_empty() {
            &self.hosts
        } else {
            &self.scope
        }
    }
    fn scope_label(&self) -> String {
        if self.scope.is_empty() {
            "ALL STACKS".into()
        } else {
            self.scope.join(",")
        }
    }
    fn remember(&mut self) {
        self.history.push(Navigation {
            scope: self.scope.clone(),
            tab: self.tab,
            filter: self.filter.clone(),
            service: self.service.clone(),
            selected: self.selected,
            selected_key: self.selected_key.clone(),
            sort: self.sort,
            descending: self.descending,
        });
    }
    fn back(&mut self, active: &watch::Sender<Vec<String>>) {
        if self.detail.is_some() {
            self.close_detail();
        } else if let Some(view) = self.history.pop() {
            self.scope = view.scope;
            self.tab = view.tab;
            self.filter = view.filter;
            self.service = view.service;
            self.selected = view.selected;
            self.selected_key = view.selected_key;
            self.sort = view.sort;
            self.descending = view.descending;
            active.send_replace(self.scope.clone());
        } else if !self.filter.is_empty() {
            self.filter.clear();
        } else if !self.scope.is_empty() {
            self.select_scope(Vec::new(), active);
        }
    }
    fn picker_options(&self) -> Vec<String> {
        std::iter::once("all".to_string())
            .chain(self.hosts.iter().cloned())
            .filter(|h| fuzzy_score(h, &self.buffer).is_some())
            .collect()
    }
    fn rows(&self) -> Vec<RowData> {
        let mut rows: Vec<_> = self
            .scoped_hosts()
            .iter()
            .flat_map(|host| self.host_rows(host))
            .collect();
        if let Some((host, service)) = &self.service {
            rows.retain(|r| &r.host == host && r.service.as_ref() == Some(service));
        }
        rows.retain(|r| {
            self.filter.split_whitespace().all(|term| {
                if let Some(value) = term
                    .strip_prefix("stack:")
                    .or_else(|| term.strip_prefix("host:"))
                {
                    value.split(',').any(|h| self.repo.normalize(h) == r.host)
                } else if let Some(value) = term.strip_prefix("state:") {
                    r.status.eq_ignore_ascii_case(value)
                        || (self.tab != 1
                            && r.container.as_ref().is_some_and(|c| {
                                c.state.eq_ignore_ascii_case(value)
                                    || (value == "stopped" && c.state == "exited")
                                    || (value == "unhealthy" && c.status.contains("(unhealthy)"))
                                    || (value == "healthy" && c.status.contains("(healthy)"))
                            }))
                        || self.states[&r.host].label().eq_ignore_ascii_case(value)
                } else {
                    fuzzy_score(&r.cells.join(" "), term).is_some()
                }
            })
        });
        if self.tab == 7 {
            rows.sort_by_key(|r| std::cmp::Reverse(r.event_sequence));
            if self.descending {
                rows.reverse();
            }
        } else {
            rows.sort_by(|a, b| {
                let order = match self.sort {
                    1 => a.host.cmp(&b.host).then_with(|| a.key.cmp(&b.key)),
                    2 => status_rank(&a.status)
                        .cmp(&status_rank(&b.status))
                        .then_with(|| a.key.cmp(&b.key)),
                    _ => a
                        .cells
                        .get(usize::from(self.tab != 0))
                        .cmp(&b.cells.get(usize::from(self.tab != 0)))
                        .then_with(|| a.host.cmp(&b.host)),
                };
                if self.descending {
                    order.reverse()
                } else {
                    order
                }
            });
        }
        rows
    }
    fn host_rows(&self, host: &str) -> Vec<RowData> {
        let mut rows = Vec::new();
        let state = &self.states[host];
        let containers = &state.snapshot.containers;
        let make = |key: String,
                    mut cells: Vec<String>,
                    status: String,
                    service: Option<String>,
                    container: Option<Container>,
                    port: Option<Port>| {
            if self.tab != 0 {
                cells.insert(0, host.into());
                cells.push(state.label().into());
            }
            RowData {
                key: if self.tab == 0 {
                    host.into()
                } else {
                    format!("{host}/{key}")
                },
                host: host.into(),
                cells,
                status,
                service,
                container,
                port,
                event_sequence: 0,
            }
        };
        match self.tab {
            0 => {
                let s = &self.states[host];
                let running = s
                    .snapshot
                    .containers
                    .iter()
                    .filter(|c| c.state == "running")
                    .count();
                rows.push(make(
                    host.into(),
                    vec![
                        host.into(),
                        s.label().into(),
                        format!("{running}/{}", s.snapshot.containers.len()),
                        s.seen
                            .map_or("never".into(), |t| format!("{}s", t.elapsed().as_secs())),
                        containers
                            .iter()
                            .filter(|c| c.status.contains("unhealthy"))
                            .count()
                            .to_string(),
                        if s.config_error.is_some() {
                            "?".into()
                        } else {
                            s.desired
                                .iter()
                                .filter(|service| {
                                    !containers
                                        .iter()
                                        .any(|c| c.project == host && &c.service == *service)
                                })
                                .count()
                                .to_string()
                        },
                        if self.busy.contains_key(host) {
                            "BUSY".into()
                        } else {
                            String::new()
                        },
                        s.error
                            .as_ref()
                            .or(s.config_error.as_ref())
                            .cloned()
                            .unwrap_or_default(),
                    ],
                    s.label().into(),
                    None,
                    None,
                    None,
                ));
            }
            1 => {
                let mut services: BTreeMap<String, Vec<&Container>> = state
                    .desired
                    .iter()
                    .map(|s| (s.clone(), Vec::new()))
                    .collect();
                for c in containers
                    .iter()
                    .filter(|c| c.project == host && !c.service.is_empty())
                {
                    services.entry(c.service.clone()).or_default().push(c);
                }
                for (service, group) in services {
                    let status = if group.is_empty() {
                        "missing"
                    } else if group.iter().any(|c| c.status.contains("unhealthy")) {
                        "unhealthy"
                    } else if group.iter().all(|c| c.state == "running") {
                        "running"
                    } else {
                        "stopped"
                    };
                    let running = group.iter().filter(|c| c.state == "running").count();
                    let samples: Option<Vec<_>> = group
                        .iter()
                        .filter(|c| c.state == "running")
                        .map(|c| {
                            state
                                .stats
                                .get(&c.id)
                                .filter(|(_, t)| t.elapsed() < Duration::from_secs(10))
                                .map(|(s, _)| s)
                        })
                        .collect();
                    let samples = samples.filter(|s| !s.is_empty() && state.live());
                    let cpu = samples.as_ref().map_or("—".into(), |s| {
                        format!("{:.1}%", s.iter().map(|s| s.cpu).sum::<f64>())
                    });
                    let memory = samples
                        .as_ref()
                        .map_or("—".into(), |s| bytes(s.iter().map(|s| s.memory).sum()));
                    rows.push(make(
                        service.clone(),
                        vec![
                            service.clone(),
                            status.into(),
                            format!("{running}/{}", group.len()),
                            cpu,
                            memory,
                            group.first().map_or(String::new(), |c| c.image.clone()),
                        ],
                        status.into(),
                        Some(service),
                        group.first().map(|c| (*c).clone()),
                        None,
                    ));
                }
            }
            2 => {
                for c in containers {
                    let stat = state
                        .stats
                        .get(&c.id)
                        .filter(|(_, t)| state.live() && t.elapsed() < Duration::from_secs(10));
                    let (cpu, mem, net) =
                        stat.map_or(("—".into(), "—".into(), "—".into()), |(s, _)| {
                            (
                                format!("{:.1}%", s.cpu),
                                format!("{} / {}", bytes(s.memory), bytes(s.limit)),
                                format!("{} / {}", bytes(s.rx), bytes(s.tx)),
                            )
                        });
                    rows.push(make(
                        c.id.clone(),
                        vec![
                            c.name.clone(),
                            c.status.clone(),
                            cpu,
                            mem,
                            net,
                            c.project.clone(),
                        ],
                        c.status.clone(),
                        (!c.service.is_empty() && c.project == host).then(|| c.service.clone()),
                        Some(c.clone()),
                        None,
                    ));
                }
            }
            3 => {
                for v in &state.snapshot.images {
                    let id = engine::string(v, "Id");
                    let tags = v["RepoTags"]
                        .as_array()
                        .map(|a| {
                            a.iter()
                                .filter_map(|v| v.as_str())
                                .collect::<Vec<_>>()
                                .join(", ")
                        })
                        .unwrap_or_default();
                    rows.push(make(
                        id.clone(),
                        vec![tags, id, bytes(v["Size"].as_u64().unwrap_or(0))],
                        String::new(),
                        None,
                        None,
                        None,
                    ));
                }
            }
            4 => {
                for v in &state.snapshot.volumes {
                    let name = engine::string(v, "Name");
                    rows.push(make(
                        name.clone(),
                        vec![
                            name,
                            engine::string(v, "Driver"),
                            engine::string(v, "Mountpoint"),
                        ],
                        String::new(),
                        None,
                        None,
                        None,
                    ));
                }
            }
            5 => {
                for v in &state.snapshot.networks {
                    let id = engine::string(v, "Id");
                    rows.push(make(
                        id,
                        vec![
                            engine::string(v, "Name"),
                            engine::string(v, "Driver"),
                            engine::string(v, "Scope"),
                        ],
                        String::new(),
                        None,
                        None,
                        None,
                    ));
                }
            }
            6 => {
                for c in containers {
                    for p in &c.ports {
                        rows.push(make(
                            format!(
                                "{}:{}:{}:{}",
                                c.id,
                                p.private,
                                p.public.unwrap_or(0),
                                p.protocol
                            ),
                            vec![
                                c.name.clone(),
                                format!("{}/{}", p.private, p.protocol),
                                p.public
                                    .map_or("unpublished".into(), |v| format!("{}:{v}", p.ip)),
                                if p.protocol == "tcp" {
                                    "f → local tunnel".into()
                                } else {
                                    "UDP".into()
                                },
                            ],
                            c.status.clone(),
                            (!c.service.is_empty() && c.project == host).then(|| c.service.clone()),
                            Some(c.clone()),
                            Some(p.clone()),
                        ));
                    }
                }
            }
            7 => {
                for (sequence, event) in &state.events {
                    let mut row = make(
                        sequence.to_string(),
                        vec![event.clone()],
                        String::new(),
                        None,
                        None,
                        None,
                    );
                    row.event_sequence = *sequence;
                    rows.push(row);
                }
            }
            _ => {}
        }
        rows
    }
    fn selected_row(&self) -> Option<RowData> {
        if self.detail.is_some() {
            self.detail_row.clone()
        } else {
            let rows = self.rows();
            self.selected_key
                .as_ref()
                .and_then(|key| rows.iter().find(|r| &r.key == key))
                .or_else(|| rows.get(self.selected))
                .cloned()
        }
    }
    fn stabilize(&mut self) {
        if self.detail.is_some() {
            return;
        }
        let rows = self.rows();
        if let Some(key) = &self.selected_key
            && let Some(index) = rows.iter().position(|r| &r.key == key)
        {
            self.selected = index;
        }
        self.selected = self.selected.min(rows.len().saturating_sub(1));
        self.selected_key = rows.get(self.selected).map(|r| r.key.clone());
    }
    fn move_by(&mut self, delta: isize) {
        if self.detail.is_some() {
            if self.follow && self.logs {
                self.scroll = self.lines.len().saturating_sub(self.viewport_height);
            }
            self.follow = false;
            self.scroll = self.scroll.saturating_add_signed(delta);
            return;
        }
        let count = self.rows().len();
        self.selected = self
            .selected
            .saturating_add_signed(delta)
            .min(count.saturating_sub(1));
        self.selected_key = self.rows().get(self.selected).map(|r| r.key.clone());
    }
    fn close_detail(&mut self) {
        if self.detail.is_some() {
            self.filter = std::mem::take(&mut self.detail_filter);
        }
        self.detail = None;
        self.detail_row = None;
        self.operation_host = None;
        self.logs = false;
        self.detail_task = None;
        self.detail_id = None;
        self.detail_service = None;
        self.generation += 1;
        self.scroll = 0;
    }
    fn select_tab(&mut self, index: usize) {
        self.close_detail();
        self.tab = index % TABS.len();
        self.service = None;
        self.history.clear();
        self.selected = 0;
        self.selected_key = None;
        self.filter.clear();
        self.notice.clear();
    }
    fn select_scope(&mut self, scope: Vec<String>, active: &watch::Sender<Vec<String>>) {
        self.close_detail();
        self.scope = scope;
        self.service = None;
        self.history.clear();
        self.selected = 0;
        self.selected_key = None;
        self.notice.clear();
        active.send_replace(self.scope.clone());
    }
    fn cycle_scope(&mut self, forward: bool, active: &watch::Sender<Vec<String>>) {
        let index = if self.scope.len() == 1 {
            self.hosts
                .iter()
                .position(|h| h == &self.scope[0])
                .map_or(0, |i| i + 1)
        } else {
            0
        };
        let count = self.hosts.len() + 1;
        let next = if forward {
            (index + 1) % count
        } else {
            (index + count - 1) % count
        };
        self.select_scope(
            if next == 0 {
                vec![]
            } else {
                vec![self.hosts[next - 1].clone()]
            },
            active,
        );
    }
    fn drill_down(&mut self, active: &watch::Sender<Vec<String>>) {
        let Some(row) = self.selected_row() else {
            return;
        };
        self.remember();
        if self.tab == 0 {
            self.scope = vec![row.host];
            self.tab = 1;
            self.service = None;
            active.send_replace(self.scope.clone());
        } else if self.tab == 1 {
            self.tab = 2;
            self.scope = vec![row.host.clone()];
            active.send_replace(self.scope.clone());
            self.service = row.service.map(|s| (row.host, s));
        }
        self.filter.clear();
        self.selected = 0;
        self.selected_key = None;
    }
    fn open_detail(&mut self, logs: bool, tx: &mpsc::Sender<Update>) {
        if let Some(row) = self.selected_row()
            && let Some(c) = row.container.clone()
        {
            self.close_detail();
            self.detail_filter = std::mem::take(&mut self.filter);
            self.detail_row = Some(row.clone());
            self.logs = logs;
            self.detail = Some("Loading…".into());
            self.lines.clear();
            self.log_bytes = 0;
            self.dropped = 0;
            self.follow = true;
            self.filter.clear();
            self.detail_id = Some(c.id.clone());
            self.detail_service = Some(c.service);
            self.detail_task = Some(engine::detail(
                self.repo.clone(),
                row.host,
                c.id,
                self.generation,
                logs,
                tx.clone(),
            ));
        } else {
            self.notice = "Select a live container first".into();
        }
    }
    fn request(&mut self, action: &str, extra: Vec<String>) {
        let Some(row) = self.selected_row() else {
            self.notice = "Select a target first".into();
            return;
        };
        let host = row.host;
        if action != "validate" && !self.states[&host].live() {
            self.notice = "Target is offline or stale".into();
            return;
        }
        if self.busy.contains_key(&host) {
            self.notice = "An operation is already active for this stack".into();
            return;
        }
        let services = if self.tab == 0 || matches!(action, "validate" | "doctor") {
            vec![]
        } else {
            self.selected_row()
                .and_then(|r| r.service)
                .into_iter()
                .collect()
        };
        if self.tab != 0 && !matches!(action, "validate" | "doctor") && services.is_empty() {
            self.notice =
                "Select a managed service, or use Stacks for a whole-stack operation".into();
            return;
        }
        if !matches!(
            action,
            "validate" | "doctor" | "deploy" | "update" | "pull" | "build"
        ) && services.is_empty()
        {
            self.notice = "Select a managed Compose service or container".into();
            return;
        }
        self.pending = Some(Pending {
            host: host.clone(),
            action: action.into(),
            services,
            extra,
            token: host,
        });
        self.buffer.clear();
        self.input = Some('!');
    }
    fn append_log(&mut self, text: String) {
        for line in text.lines() {
            self.log_bytes += line.len();
            self.lines.push_back(line.to_string());
        }
        while self.lines.len() > 5000 || self.log_bytes > 5 * 1024 * 1024 {
            if let Some(line) = self.lines.pop_front() {
                self.log_bytes -= line.len();
                self.dropped += 1;
                self.scroll = self.scroll.saturating_sub(1);
            }
        }
    }
}

fn bytes(n: u64) -> String {
    if n >= 1024 * 1024 * 1024 {
        format!("{:.1}GiB", n as f64 / 1073741824.0)
    } else if n >= 1024 * 1024 {
        format!("{:.1}MiB", n as f64 / 1048576.0)
    } else {
        format!("{:.1}KiB", n as f64 / 1024.0)
    }
}
fn fuzzy_score(text: &str, query: &str) -> Option<usize> {
    let text = text.to_lowercase();
    let query = query.to_lowercase();
    let mut chars = query.chars();
    let mut next = chars.next();
    let mut score = 0;
    for (i, c) in text.chars().enumerate() {
        if Some(c) == next {
            score += i;
            next = chars.next();
            if next.is_none() {
                return Some(score);
            }
        }
    }
    next.is_none().then_some(score)
}
fn key_name(key: KeyEvent) -> String {
    let name = match key.code {
        KeyCode::Char(c) => c.to_string(),
        KeyCode::Enter => "enter".into(),
        KeyCode::Esc => "esc".into(),
        KeyCode::Tab => "tab".into(),
        KeyCode::BackTab => "shift-tab".into(),
        KeyCode::Up => "up".into(),
        KeyCode::Down => "down".into(),
        KeyCode::Left => "left".into(),
        KeyCode::Right => "right".into(),
        KeyCode::Home => "home".into(),
        KeyCode::End => "end".into(),
        KeyCode::PageUp => "pageup".into(),
        KeyCode::PageDown => "pagedown".into(),
        _ => String::new(),
    };
    if key.modifiers.contains(KeyModifiers::CONTROL) {
        format!("ctrl-{name}")
    } else {
        name
    }
}
fn binding(key: &str) -> &str {
    match key {
        "q" | "ctrl-c" => "quit",
        "j" | "down" => "down",
        "k" | "up" => "up",
        "g" | "home" => "first",
        "G" | "end" => "last",
        "ctrl-u" | "pageup" => "page-up",
        "ctrl-d" | "pagedown" => "page-down",
        "h" | "left" | "shift-tab" => "previous-tab",
        "l" | "right" | "tab" => "next-tab",
        "b" | "ctrl-b" => "scope",
        "p" => "preview",
        "o" => "sort",
        "O" => "reverse-sort",
        "[" => "previous-host",
        "]" => "next-host",
        "/" => "filter",
        ":" => "command",
        "?" => "help",
        "enter" => "open",
        "esc" => "back",
        "L" => "logs",
        "i" => "inspect",
        "c" => "config",
        "t" => "stats",
        "e" => "shell",
        "a" => "actions",
        "r" => "restart",
        "s" => "stop",
        "S" => "start",
        "D" => "deploy",
        "U" => "update",
        "x" => "remove",
        "R" => "refresh",
        "f" => "forward",
        other => other,
    }
}

pub async fn run(repo: Repo) -> Result<()> {
    ensure!(
        std::io::stdin().is_terminal() && std::io::stdout().is_terminal(),
        "The TUI needs a terminal. Try tctl status or tctl --help."
    );
    let valid = [
        "quit",
        "down",
        "up",
        "first",
        "last",
        "page-up",
        "page-down",
        "previous-tab",
        "next-tab",
        "sidebar",
        "scope",
        "preview",
        "sort",
        "reverse-sort",
        "previous-host",
        "next-host",
        "filter",
        "command",
        "help",
        "open",
        "back",
        "logs",
        "inspect",
        "config",
        "stats",
        "shell",
        "actions",
        "restart",
        "stop",
        "start",
        "deploy",
        "update",
        "remove",
        "refresh",
        "forward",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
    ];
    for action in repo.keys.values() {
        ensure!(
            valid.contains(&action.as_str()),
            "Unknown keybinding action: {action}"
        );
    }
    let mut app = App::new(repo);
    let (tx, mut rx) = mpsc::channel(512);
    let (active_tx, active) = watch::channel(app.scope.clone());
    let (refresh_tx, refresh) = watch::channel(0u64);
    let _workers: Vec<_> = app
        .hosts
        .iter()
        .map(|name| {
            Abort(engine::watch_host(
                name.clone(),
                app.repo.hosts[name].clone(),
                tx.clone(),
                active.clone(),
                refresh.clone(),
            ))
        })
        .collect();
    let _desired = watch_desired(app.repo.clone(), tx.clone(), refresh.clone());
    let mut terminal = ratatui::init();
    let mut events = Some(EventStream::new());
    let mut tick = tokio::time::interval(Duration::from_millis(33));
    let result:Result<()>=async {
        loop {
            tokio::select! {
                _=tick.tick()=>{
                    app.viewport_height = terminal.size()?.height.saturating_sub(6) as usize;
                    app.stabilize();terminal.draw(|f|draw(f,&app))?;
                    app.tunnels.retain(|port,child| {
                        if child.try_wait().ok().flatten().is_some() {
                            app.notice = format!("SSH forward on {port} closed"); false
                        } else { true }
                    });
                },
                _=tokio::signal::ctrl_c()=>{if app.busy.is_empty(){break;}else{app.notice="Use :cancel before quitting".into();}},
                update=rx.recv()=>{if let Some(update)=update {handle_update(&mut app,update,&tx,&refresh_tx);}},
                event=events.as_mut().expect("event stream active").next()=>{
                    let Some(event)=event else {break;};
                    if let Event::Key(key)=event? {
                        if key.kind!=KeyEventKind::Press {continue;}
                        if handle_key(&mut app,key,&tx,&active_tx,&refresh_tx,&mut terminal,&mut events).await? {break;}
                    }
                }
            }
        } Ok(())
    }.await;
    ratatui::restore();
    for child in app.tunnels.values_mut() {
        let _ = child.kill().await;
    }
    // Children are killed on drop; never leave a local forward listening after exit.
    result
}

fn handle_update(
    app: &mut App,
    update: Update,
    tx: &mpsc::Sender<Update>,
    refresh: &watch::Sender<u64>,
) {
    match update {
        Update::Snapshot(name, snapshot) => {
            if app.detail_row.as_ref().is_some_and(|r| r.host == name)
                && app.logs
                && let Some(id) = &app.detail_id
                && !snapshot.containers.iter().any(|c| &c.id == id)
                && let Some(c) = snapshot
                    .containers
                    .iter()
                    .find(|c| c.project == name && Some(&c.service) == app.detail_service.as_ref())
            {
                app.generation += 1;
                app.detail_id = Some(c.id.clone());
                if let Some(row) = &mut app.detail_row {
                    row.container = Some(c.clone());
                }
                app.append_log("--- container recreated; following replacement ---".into());
                app.detail_task = Some(engine::detail(
                    app.repo.clone(),
                    name.clone(),
                    c.id.clone(),
                    app.generation,
                    true,
                    tx.clone(),
                ));
            }
            let state = app.states.get_mut(&name).unwrap();
            state.snapshot = snapshot;
            state.seen = Some(Instant::now());
            state.error = None;
            state.stats.retain(|id, _| {
                state
                    .snapshot
                    .containers
                    .iter()
                    .any(|c| &c.id == id && c.state == "running")
            });
        }
        Update::Error(name, error) => app.states.get_mut(&name).unwrap().error = Some(error),
        Update::Event(name, event) => {
            let events = &mut app.states.get_mut(&name).unwrap().events;
            app.event_sequence += 1;
            events.push_back((app.event_sequence, event));
            if events.len() > 1000 {
                events.pop_front();
            }
        }
        Update::Stats(name, id, stats) => {
            app.states
                .get_mut(&name)
                .unwrap()
                .stats
                .insert(id, (stats, Instant::now()));
        }
        Update::Log(generation, text) if generation == app.generation => app.append_log(text),
        Update::Detail(generation, text) if generation == app.generation => app.detail = Some(text),
        Update::Done(message) => {
            if let Some((host, _)) = message.split_once(':') {
                app.busy.remove(host);
                app.cancellations.remove(host);
            }
            app.notice = message;
            refresh.send_modify(|v| *v += 1);
        }
        Update::Desired(name, result) => {
            let s = app.states.get_mut(&name).unwrap();
            match result {
                Ok(services) => {
                    s.desired = services;
                    s.config_error = None
                }
                Err(e) => {
                    s.config_error = Some(e);
                    s.desired.clear();
                }
            }
        }
        Update::Tunnel(port, child) => {
            app.tunnels.insert(port, child);
            app.notice =
                format!("Forward listening on 127.0.0.1:{port}. :unforward {port} to stop");
        }
        _ => {}
    }
}

async fn handle_key(
    app: &mut App,
    key: KeyEvent,
    tx: &mpsc::Sender<Update>,
    active: &watch::Sender<Vec<String>>,
    refresh: &watch::Sender<u64>,
    terminal: &mut ratatui::DefaultTerminal,
    events: &mut Option<EventStream>,
) -> Result<bool> {
    if let Some(mode) = app.input {
        if mode == '@' {
            match key.code {
                KeyCode::Esc => {
                    app.input = None;
                    app.buffer.clear();
                }
                KeyCode::Down => {
                    app.picker_index =
                        (app.picker_index + 1).min(app.picker_options().len().saturating_sub(1))
                }
                KeyCode::Up => app.picker_index = app.picker_index.saturating_sub(1),
                KeyCode::Enter => {
                    if let Some(host) = app.picker_options().get(app.picker_index).cloned() {
                        app.select_scope(if host == "all" { vec![] } else { vec![host] }, active);
                        app.input = None;
                        app.buffer.clear();
                    }
                }
                KeyCode::Backspace => {
                    app.buffer.pop();
                    app.picker_index = 0;
                }
                KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                    app.buffer.push(c);
                    app.picker_index = 0;
                }
                _ => {}
            }
            return Ok(false);
        }
        match key.code {
            KeyCode::Esc => {
                app.input = None;
                app.pending = None;
                app.buffer.clear();
            }
            KeyCode::Enter => {
                let text = std::mem::take(&mut app.buffer);
                app.input = None;
                match mode {
                    '/' => {
                        app.filter = text;
                        if app.detail.is_none() {
                            app.selected = 0;
                            app.selected_key = None;
                        }
                    }
                    ':' => {
                        if command(app, &text, tx, active, refresh) {
                            return Ok(true);
                        }
                    }
                    '!' => {
                        if let Some(pending) = app.pending.take() {
                            if text == pending.token {
                                launch(app, pending, tx, terminal, events).await?;
                            } else {
                                app.notice = "Canceled: target did not match".into();
                            }
                        }
                    }
                    _ => {}
                }
            }
            KeyCode::Backspace => {
                app.buffer.pop();
            }
            KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                app.buffer.push(c);
            }
            _ => {}
        }
        if mode == '/' && app.input.is_some() {
            app.filter = app.buffer.clone();
            if app.detail.is_none() {
                app.selected = 0;
                app.selected_key = None;
            }
        }
        return Ok(false);
    }
    if app.help {
        if matches!(
            key.code,
            KeyCode::Esc | KeyCode::Char('?') | KeyCode::Char('q')
        ) {
            app.help = false;
        } else if matches!(key.code, KeyCode::Down | KeyCode::Char('j')) {
            app.scroll += 1;
        } else if matches!(key.code, KeyCode::Up | KeyCode::Char('k')) {
            app.scroll = app.scroll.saturating_sub(1);
        }
        return Ok(false);
    }
    let name = key_name(key);
    let action = app
        .repo
        .keys
        .get(&name)
        .map(String::as_str)
        .unwrap_or_else(|| binding(&name))
        .to_string();
    if let Some(index) = app.menu {
        let options = action_menu();
        match action.as_str() {
            "down" => app.menu = Some((index + 1) % options.len()),
            "up" => app.menu = Some(index.saturating_sub(1)),
            "back" | "quit" => app.menu = None,
            "open" => {
                app.menu = None;
                app.request(options[index], vec![]);
            }
            _ => {}
        }
        return Ok(false);
    }
    match action.as_str() {
        "quit" => {
            if app.busy.is_empty() {
                return Ok(true);
            }
            app.notice =
                "An operation is running. Use :cancel on its host, wait for it to stop, then quit."
                    .into();
        }
        "down" => app.move_by(1),
        "up" => app.move_by(-1),
        "page-down" => app.move_by(10),
        "page-up" => app.move_by(-10),
        "first" => {
            app.selected = 0;
            app.selected_key = None;
            app.scroll = 0;
            app.follow = false;
        }
        "last" => {
            app.selected = app.rows().len().saturating_sub(1);
            app.selected_key = None;
            app.follow = true;
        }
        "next-tab" => app.select_tab(app.tab + 1),
        "previous-tab" => app.select_tab((app.tab + TABS.len() - 1) % TABS.len()),
        "sidebar" | "scope" => {
            app.input = Some('@');
            app.buffer.clear();
            app.picker_index = 0;
        }
        "preview" => app.preview = !app.preview,
        "sort" => {
            app.sort = (app.sort + 1) % 3;
        }
        "reverse-sort" => app.descending = !app.descending,
        "previous-host" => app.cycle_scope(false, active),
        "next-host" => app.cycle_scope(true, active),
        "filter" => {
            app.input = Some('/');
            app.buffer = app.filter.clone();
        }
        "command" => {
            app.input = Some(':');
            app.buffer.clear();
        }
        "help" => {
            app.help = true;
            app.scroll = 0;
        }
        "back" => app.back(active),
        "open" if app.detail.is_some() => {}
        "open" => match app.tab {
            0 | 1 => app.drill_down(active),
            2 => app.open_detail(true, tx),
            6 => {
                app.input = Some(':');
                app.buffer = "forward ".into();
            }
            _ => {}
        },
        "logs" => app.open_detail(true, tx),
        "inspect" => app.open_detail(false, tx),
        "config" => open_config(app, tx),
        "stats" => app.select_tab(2),
        "actions" => app.menu = Some(0),
        "restart" | "stop" | "start" | "deploy" | "update" | "remove" | "shell" => {
            app.request(&action, vec![])
        }
        "refresh" => {
            refresh.send_modify(|v| *v += 1);
            if app.detail.is_some() {
                let logs = app.logs;
                app.open_detail(logs, tx);
            }
        }
        "forward" => {
            if app.logs {
                if app.follow {
                    app.scroll = app.lines.len().saturating_sub(app.viewport_height);
                }
                app.follow = !app.follow;
            } else if app.tab == 6 {
                app.input = Some(':');
                app.buffer = "forward ".into();
            } else {
                app.select_tab(6);
            }
        }
        n => {
            if let Ok(n) = n.parse::<usize>()
                && (1..=TABS.len()).contains(&n)
            {
                app.select_tab(n - 1);
            }
        }
    }
    Ok(false)
}
fn open_config(app: &mut App, tx: &mpsc::Sender<Update>) {
    let Some(row) = app.selected_row() else {
        app.notice = "Select a target first".into();
        return;
    };
    let stack = row.host.clone();
    let services: Vec<_> = if app.tab == 0 {
        vec![]
    } else {
        app.selected_row()
            .and_then(|r| r.service)
            .into_iter()
            .collect()
    };
    app.close_detail();
    app.detail_filter = std::mem::take(&mut app.filter);
    app.detail_row = Some(row);
    app.detail = Some("Resolving Compose configuration…".into());
    let mut repo = app.repo.clone();
    let tx = tx.clone();
    let generation = app.generation;
    app.detail_task = Some(Abort(tokio::spawn(async move {
        let result: Result<String> = async {
            repo.environment = crate::actions::vault_environment(repo.host(&stack)?).await?;
            crate::actions::effective_config(&repo, &stack, &services).await
        }
        .await;
        let text = match result {
            Ok(text) => text,
            Err(e) => format!("{e:#}"),
        };
        let _ = tx
            .send(Update::Detail(generation, engine::clean(&text)))
            .await;
    })));
}

fn action_menu() -> Vec<&'static str> {
    vec![
        "validate", "doctor", "deploy", "update", "pull", "build", "restart", "start", "stop",
        "remove", "shell",
    ]
}
fn command(
    app: &mut App,
    text: &str,
    tx: &mpsc::Sender<Update>,
    active: &watch::Sender<Vec<String>>,
    refresh: &watch::Sender<u64>,
) -> bool {
    let args: Vec<_> = text.split_whitespace().collect();
    let Some(cmd) = args.first() else {
        return false;
    };
    if let Some(index) = TABS.iter().position(|t| t.eq_ignore_ascii_case(cmd)) {
        app.select_tab(index);
        return false;
    }
    match *cmd {
        "q" | "quit" => {
            if app.busy.is_empty() {
                return true;
            }
            app.notice = "An operation is running. Use :cancel before quitting.".into();
        }
        "cancel" => {
            let Some(name) = args
                .get(1)
                .map(|s| app.repo.normalize(s).to_string())
                .or_else(|| app.operation_host.clone())
                .or_else(|| app.selected_row().map(|r| r.host))
            else {
                app.notice = "Usage: :cancel STACK".into();
                return false;
            };
            if let Some(cancel) = app.cancellations.remove(&name) {
                let _ = cancel.send(());
                app.notice =
                    "Cancel requested. Changes already applied are not rolled back.".into();
            } else {
                app.notice = "No operation running on this host".into();
            }
        }
        "config" => open_config(app, tx),
        "ctx" => {
            if let Some(value) = args.get(1) {
                let scope: Vec<String> = if *value == "all" {
                    vec![]
                } else {
                    value
                        .split(',')
                        .map(|s| app.repo.normalize(s).to_string())
                        .collect()
                };
                if scope.iter().all(|h| app.states.contains_key(h)) {
                    let mut scope = scope;
                    scope.sort();
                    scope.dedup();
                    app.select_scope(scope, active);
                } else {
                    app.notice = "Unknown stack. Use :ctx all or :ctx nexus,stargate".into();
                }
            } else {
                app.input = Some('@');
                app.buffer.clear();
                app.picker_index = 0;
            }
        }
        "sort" => {
            if let Some(index) = args
                .get(1)
                .and_then(|s| ["name", "stack", "state"].iter().position(|v| v == s))
            {
                app.sort = index;
                app.descending = args.get(2) == Some(&"desc");
            } else {
                app.notice = "Usage: :sort name|stack|state [desc]".into();
            }
        }
        "refresh" => {
            refresh.send_modify(|v| *v += 1);
        }
        "scale" => {
            if let Some(count) = args.get(1).and_then(|s| s.parse::<u32>().ok()) {
                app.request("scale", vec!["--replicas".into(), count.to_string()]);
            } else {
                app.notice = "Usage: :scale COUNT".into();
            }
        }
        "forward" => {
            if let Some(port) = args
                .get(1)
                .and_then(|s| s.parse::<u16>().ok())
                .filter(|p| *p > 0)
            {
                if app.tunnels.contains_key(&port) {
                    app.notice = "That local port already has a tctl tunnel".into();
                } else if let Some(row) = app.selected_row().filter(|r| r.port.is_some()) {
                    start_forward(app, port, row, tx);
                } else {
                    app.notice = "Select a TCP port in the Ports tab".into();
                }
            } else {
                app.notice = "Usage: :forward LOCAL_PORT with a port selected".into();
            }
        }
        "unforward" => {
            if let Some(port) = args.get(1).and_then(|s| s.parse::<u16>().ok()) {
                app.tunnels.remove(&port);
                app.notice = format!("Closed tunnel on {port}");
            }
        }
        "tunnels" => {
            app.notice = if app.tunnels.is_empty() {
                "No active tunnels".into()
            } else {
                format!(
                    "Local ports: {:?}; :unforward PORT to close",
                    app.tunnels.keys().collect::<Vec<_>>()
                )
            };
        }
        action if action_menu().contains(&action) => app.request(action, vec![]),
        _ => app.notice = format!("Unknown command: {text}"),
    }
    false
}

async fn launch(
    app: &mut App,
    pending: Pending,
    tx: &mpsc::Sender<Update>,
    terminal: &mut ratatui::DefaultTerminal,
    events: &mut Option<EventStream>,
) -> Result<()> {
    if pending.action != "validate" && !app.states[&pending.host].live() {
        app.notice = "Target became stale. Refresh and retry.".into();
        return Ok(());
    }
    if app.busy.contains_key(&pending.host) {
        app.notice = "Another action is running for that stack".into();
        return Ok(());
    }
    let mut cmd = Command::new(std::env::current_exe()?);
    cmd.arg("--root")
        .arg(&app.repo.root)
        .args(["--yes", &pending.action, &pending.host])
        .args(&pending.services)
        .args(&pending.extra)
        .kill_on_drop(true);
    if pending.action == "shell" {
        // Stop the UI input reader before handing the terminal to Docker exec.
        events.take();
        ratatui::restore();
        let status = cmd.status().await;
        *terminal = ratatui::init();
        *events = Some(EventStream::new());
        app.notice = format!("Shell finished: {status:?}");
        return Ok(());
    }
    let mut child = cmd
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(Stdio::null())
        .spawn()?;
    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    app.close_detail();
    app.logs = true;
    app.detail_filter = std::mem::take(&mut app.filter);
    app.operation_host = Some(pending.host.clone());
    app.detail = Some(format!("Operation output · {}", pending.host));
    app.lines.clear();
    app.log_bytes = 0;
    app.follow = true;
    let generation = app.generation;
    let (cancel_tx, cancel_rx) = oneshot::channel();
    app.cancellations.insert(pending.host.clone(), cancel_tx);
    let tx = tx.clone();
    let host = pending.host.clone();
    let task = tokio::spawn(async move {
        let tx_out = tx.clone();
        let tx_err = tx.clone();
        let read_out = async move {
            let mut lines = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                if tx_out
                    .send(Update::Log(generation, engine::clean(&line)))
                    .await
                    .is_err()
                {
                    break;
                }
            }
        };
        let read_err = async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                if tx_err
                    .send(Update::Log(generation, engine::clean(&line)))
                    .await
                    .is_err()
                {
                    break;
                }
            }
        };
        let wait = async {
            tokio::select! {
                result = child.wait() => result,
                _ = cancel_rx => {
                    if let Some(pid) = child.id() {
                        let _ = Command::new("kill").args(["-INT", &pid.to_string()]).status().await;
                    }
                    match tokio::time::timeout(Duration::from_secs(5), child.wait()).await {
                        Ok(result) => result,
                        Err(_) => { let _ = child.kill().await; child.wait().await }
                    }
                }
            }
        };
        let (status, _, _) = tokio::join!(wait, read_out, read_err);
        let result = match status {
            Ok(s) if s.success() => "completed".into(),
            Ok(s) => format!("failed ({s})"),
            Err(e) => format!("failed ({e})"),
        };
        let _ = tx
            .send(Update::Done(format!("{host}: {} {result}", pending.action)))
            .await;
    });
    app.busy.insert(pending.host, Abort(task));
    Ok(())
}

fn start_forward(app: &mut App, local: u16, row: RowData, tx: &mpsc::Sender<Update>) {
    if !app.states[&row.host].live() {
        app.notice = "Target is not live".into();
        return;
    }
    let host = app.repo.hosts[&row.host].clone();
    let tx = tx.clone();
    let Some(alias) = host.ssh().map(str::to_string) else {
        app.notice = "SSH forwarding requires an ssh:// target".into();
        return;
    };
    let port = row.port.unwrap();
    if port.protocol != "tcp" {
        app.notice = "SSH port forwarding supports TCP only".into();
        return;
    }
    let container = row.container.unwrap();
    app.notice = format!("Opening loopback-only tunnel on {local}…");
    tokio::spawn(async move {
        let result:Result<Child>=async {
            let available = std::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, local)).context("Local port is already in use")?;
            let connection=engine::Connection::open(&host).await?;
            let (address,remote)=if let Some(public)=port.public {
                let address=if port.ip=="0.0.0.0" || port.ip.is_empty(){"127.0.0.1".into()}else if port.ip=="::"{"::1".into()}else{port.ip.clone()};(address,public)
            }else{
                let inspect=connection.docker.inspect_container(&container.id,None::<bollard::query_parameters::InspectContainerOptions>).await?;
                let networks=inspect.network_settings.and_then(|n|n.networks).unwrap_or_default();
                let ips:Vec<_>=networks.values().filter_map(|n|n.ip_address.as_ref()).filter(|ip|!ip.is_empty()).collect();
                ensure!(ips.len()==1,"Unpublished port needs exactly one container network address; use a published port for multi-network containers");
                (ips[0].clone(),port.private)
            };
            let ip:std::net::IpAddr=address.parse().context("Invalid target IP")?;
            let address=match ip{std::net::IpAddr::V6(v)=>format!("[{v}]"),_=>address};
            drop(available);
            let mut child=Command::new("ssh").args(["-nNT","-o","BatchMode=yes","-o","StrictHostKeyChecking=yes","-o","ExitOnForwardFailure=yes","-o","ConnectTimeout=8","-o","ServerAliveInterval=5","-o","ServerAliveCountMax=2","-L"])
                .arg(format!("127.0.0.1:{local}:{address}:{remote}")).arg(alias).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).kill_on_drop(true).spawn()?;
            let deadline=Instant::now()+Duration::from_secs(10);
            loop {
                ensure!(child.try_wait()?.is_none(),"SSH forwarding failed; local port may already be in use");
                if tokio::net::TcpStream::connect((std::net::Ipv4Addr::LOCALHOST,local)).await.is_ok(){break;}
                ensure!(Instant::now()<deadline,"SSH forwarding timed out");
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
            Ok(child)
        }.await;
        match result {
            Ok(child) => {
                let _ = tx.send(Update::Tunnel(local, child)).await;
            }
            Err(e) => {
                let _ = tx.send(Update::Done(format!("forward: {e:#}"))).await;
            }
        }
    });
}

fn watch_desired(repo: Repo, tx: mpsc::Sender<Update>, mut refresh: watch::Receiver<u64>) -> Abort {
    Abort(tokio::spawn(async move {
        let mut stamp = 0u128;
        loop {
            let next = config_stamp(&repo.root.join("docker"));
            if next != stamp || stamp == 0 {
                stamp = next;
                for name in repo.hosts.keys() {
                    let result: Result<Vec<String>> = async {
                        repo.check_env(name)?;
                        let mut scoped = repo.clone();
                        scoped.environment =
                            crate::actions::vault_environment(&repo.hosts[name]).await?;
                        let mut command = crate::actions::compose_at(
                            &scoped,
                            name,
                            &repo.hosts[name].endpoint,
                            &["config", "--services"],
                        );
                        let output =
                            tokio::time::timeout(Duration::from_secs(10), command.output())
                                .await??;
                        ensure!(
                            output.status.success(),
                            "Compose configuration unavailable: {}",
                            engine::clean(&String::from_utf8_lossy(&output.stderr))
                        );
                        Ok(String::from_utf8(output.stdout)?
                            .lines()
                            .map(str::to_string)
                            .collect())
                    }
                    .await;
                    if tx
                        .send(Update::Desired(
                            name.clone(),
                            result.map_err(|e| format!("{e:#}")),
                        ))
                        .await
                        .is_err()
                    {
                        return;
                    }
                }
            }
            tokio::select! {_=tx.closed()=>return,_=tokio::time::sleep(Duration::from_secs(2))=>{}, _=refresh.changed()=>{stamp=0;}}
        }
    }))
}
fn config_stamp(path: &std::path::Path) -> u128 {
    let Ok(entries) = std::fs::read_dir(path) else {
        return 0;
    };
    let mut stamp = 1;
    for entry in entries.flatten() {
        let Ok(kind) = entry.file_type() else {
            continue;
        };
        if kind.is_symlink() {
            continue;
        }
        if kind.is_dir() {
            if entry.file_name() != "files" {
                stamp ^= config_stamp(&entry.path()).wrapping_mul(31);
            }
        } else if (entry.file_name() == "compose.yaml" || entry.file_name() == ".env")
            && let Ok(meta) = entry.metadata()
        {
            stamp ^= meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map_or(0, |t| t.as_nanos())
                .wrapping_add(meta.len() as u128);
        }
    }
    stamp
}

fn status_rank(status: &str) -> u8 {
    if status.contains("unhealthy") || matches!(status, "OFFLINE" | "dead") {
        0
    } else if matches!(status, "missing" | "stopped" | "STALE" | "restarting")
        || status.contains("Exited")
    {
        1
    } else if matches!(status, "CONNECTING" | "created") || status.contains("starting") {
        2
    } else {
        3
    }
}
fn status_color(status: &str) -> Color {
    match status_rank(status) {
        0 => Color::Red,
        1 | 2 => Color::Yellow,
        _ if status == "LIVE" || status == "running" || status.starts_with("Up") => Color::Green,
        _ => Color::White,
    }
}
fn table_headers(tab: usize) -> Vec<&'static str> {
    match tab {
        0 => vec![
            "STACK",
            "CONNECTION",
            "RUNNING",
            "AGE",
            "UNHEALTHY",
            "MISSING",
            "OPERATION",
            "ERROR",
        ],
        1 => vec![
            "STACK",
            "SERVICE",
            "STATE",
            "RUN/TOTAL",
            "CPU",
            "MEMORY",
            "IMAGE",
            "CONNECTION",
        ],
        2 => vec![
            "STACK",
            "CONTAINER",
            "STATUS",
            "CPU",
            "MEMORY",
            "RX / TX",
            "PROJECT",
            "CONNECTION",
        ],
        3 => vec!["HOST", "IMAGE", "ID", "SIZE", "CONNECTION"],
        4 => vec!["HOST", "VOLUME", "DRIVER", "MOUNT", "CONNECTION"],
        5 => vec!["HOST", "NETWORK", "DRIVER", "SCOPE", "CONNECTION"],
        6 => vec![
            "STACK",
            "CONTAINER",
            "PORT",
            "PUBLISHED",
            "FORWARD",
            "CONNECTION",
        ],
        _ => vec!["HOST", "DOCKER EVENTS", "CONNECTION"],
    }
}
fn visible_columns(tab: usize, width: u16) -> Vec<usize> {
    match (tab, width) {
        (0, 0..=79) => vec![0, 1, 2, 3],
        (0, 80..=109) => vec![0, 1, 2, 3, 4, 5],
        (1, 0..=64) | (2, 0..=79) => vec![0, 1, 2, 7],
        (1, 65..=89) => vec![0, 1, 2, 3, 7],
        (1, 90..=119) => vec![0, 1, 2, 3, 4, 5, 7],
        (2, 80..=109) => vec![0, 1, 2, 3, 7],
        (2, 110..=139) => vec![0, 1, 2, 3, 4, 7],
        (3..=5, 0..=79) => vec![0, 1, 4],
        (6, 0..=79) => vec![0, 1, 2, 5],
        (6, 80..=109) => vec![0, 1, 2, 3, 5],
        _ => (0..table_headers(tab).len()).collect(),
    }
}

fn draw(frame: &mut Frame, app: &App) {
    let area = frame.area();
    if area.width < 45 || area.height < 12 {
        frame.render_widget(
            Paragraph::new("tctl needs at least 45 columns × 12 rows. Resize or use tctl status."),
            area,
        );
        return;
    }
    let selected = app.selected_row();
    let state = selected.as_ref().map(|r| &app.states[&r.host]);
    let warning = state
        .and_then(|s| s.error.as_ref().or(s.config_error.as_ref()))
        .or_else(|| {
            app.scoped_hosts().iter().find_map(|h| {
                app.states[h]
                    .error
                    .as_ref()
                    .or(app.states[h].config_error.as_ref())
            })
        });
    let notice = if app.notice.is_empty() {
        warning.map(String::as_str).unwrap_or("")
    } else {
        &app.notice
    };
    let footer_height = if notice.is_empty() || app.input.is_some() {
        1
    } else {
        2
    };
    let layout = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Min(3),
        Constraint::Length(footer_height),
    ])
    .split(area);
    let hosts = app.scoped_hosts();
    let live = hosts.iter().filter(|h| app.states[*h].live()).count();
    let offline = hosts
        .iter()
        .filter(|h| app.states[*h].label() == "OFFLINE")
        .count();
    let stale = hosts
        .iter()
        .filter(|h| app.states[*h].label() == "STALE")
        .count();
    let errors = hosts
        .iter()
        .filter(|h| app.states[*h].config_error.is_some())
        .count();
    let total: usize = hosts
        .iter()
        .map(|h| app.states[h].snapshot.containers.len())
        .sum();
    let running = hosts
        .iter()
        .flat_map(|h| &app.states[h].snapshot.containers)
        .filter(|c| c.state == "running")
        .count();
    let oldest = hosts
        .iter()
        .filter_map(|h| app.states[h].seen)
        .map(|t| t.elapsed().as_secs())
        .max();
    let freshness = oldest.map_or("never".into(), |s| format!("{s}s"));
    let header = Line::from(vec![
        Span::styled(
            " tctl ",
            Style::default().fg(Color::Black).bg(ACCENT).bold(),
        ),
        Span::styled(
            format!(" {} ", app.scope_label()),
            Style::default().fg(ACCENT).bold(),
        ),
        Span::raw(format!(
            "LIVE {live}/{}  RUN {running}/{total}  ",
            hosts.len()
        )),
        Span::styled(
            format!("OFFLINE {offline}  STALE {stale}  "),
            Style::default().fg(if offline + stale > 0 {
                Color::Yellow
            } else {
                MUTED
            }),
        ),
        Span::raw(format!("age {freshness}  config errors {errors}")),
    ]);
    frame.render_widget(Paragraph::new(header), layout[0]);
    frame.render_widget(
        Tabs::new(TABS.iter().enumerate().map(|(i, t)| {
            if area.width < 100 {
                format!("{} {}", i + 1, &t[..3])
            } else {
                format!("{} {t}", i + 1)
            }
        }))
        .select(app.tab)
        .highlight_style(Style::default().fg(ACCENT).bold())
        .divider(" ")
        .padding("", ""),
        layout[1],
    );
    let mut body = layout[2];
    if app.preview && app.detail.is_none() && area.height >= 24 {
        let panes = Layout::vertical([Constraint::Min(5), Constraint::Length(4)]).split(body);
        body = panes[0];
        let text = if let Some(row) = &selected {
            let state = &app.states[&row.host];
            let age = state.seen.map_or("never".into(), |t| {
                format!("{}s ago", t.elapsed().as_secs())
            });
            let mut lines = vec![format!(
                "{} · {} · checked {age}",
                app.repo.hosts[&row.host].endpoint,
                state.label()
            )];
            if let Some(c) = &row.container {
                lines.push(format!(
                    "{} · {} · image {} · project {}",
                    c.name, c.status, c.image, c.project
                ));
                lines.push(format!(
                    "Ports: {}",
                    c.ports
                        .iter()
                        .map(|p| format!(
                            "{} → {}/{}",
                            p.public.map_or("unpublished".into(), |v| v.to_string()),
                            p.private,
                            p.protocol
                        ))
                        .collect::<Vec<_>>()
                        .join(", ")
                ));
            } else {
                lines.push(row.cells.join(" · "));
                lines.push(
                    state
                        .events
                        .back()
                        .map_or("No recent events".into(), |(_, e)| {
                            format!("Latest host event: {e}")
                        }),
                );
            }
            if let Some(error) = state.error.as_ref().or(state.config_error.as_ref()) {
                lines[1] = error.clone();
            }
            lines.join("\n")
        } else {
            "No matching resources. / edits the filter; b changes stack scope.".into()
        };
        frame.render_widget(
            Paragraph::new(engine::clean(&text)).block(
                Block::default()
                    .borders(Borders::TOP)
                    .title(format!(
                        " {} · p hide details ",
                        selected.as_ref().map_or("Details", |r| r.key.as_str())
                    ))
                    .border_style(Style::default().fg(MUTED)),
            ),
            panes[1],
        );
    }
    if let Some(detail) = &app.detail {
        let text = if app.logs {
            let lines: Vec<_> = app
                .lines
                .iter()
                .filter(|l| {
                    app.filter.is_empty() || l.to_lowercase().contains(&app.filter.to_lowercase())
                })
                .map(String::as_str)
                .collect();
            if lines.is_empty() {
                detail.clone()
            } else {
                lines.join("\n")
            }
        } else {
            detail.clone()
        };
        let count = text.lines().count();
        let height = body.height.saturating_sub(2) as usize;
        let offset = if app.follow && app.logs {
            count.saturating_sub(height)
        } else {
            app.scroll.min(count.saturating_sub(1))
        };
        let target = app
            .detail_row
            .as_ref()
            .map(|r| r.key.as_str())
            .or(app.operation_host.as_deref())
            .unwrap_or("Details");
        let title = if app.logs {
            format!(
                " {target} · Logs · {} · dropped {} · / search · Esc back ",
                if app.follow { "FOLLOW" } else { "PAUSED" },
                app.dropped
            )
        } else {
            format!(" {target} · Inspect · Esc back ")
        };
        frame.render_widget(
            Paragraph::new(text)
                .scroll((offset.min(u16::MAX as usize) as u16, 0))
                .block(
                    Block::bordered()
                        .title(title)
                        .border_style(Style::default().fg(ACCENT)),
                ),
            body,
        );
    } else {
        let headers = table_headers(app.tab);
        let columns = visible_columns(app.tab, area.width);
        let rows = app.rows();
        let count = rows.len();
        let total: usize = hosts.iter().map(|h| app.host_rows(h).len()).sum();
        let widths: Vec<_> = columns
            .iter()
            .map(|&i| match headers[i] {
                "STACK" | "HOST" => Constraint::Length(12),
                "CONNECTION" => Constraint::Length(10),
                "STATE" => Constraint::Length(11),
                "RUN/TOTAL" | "RUNNING" => Constraint::Length(9),
                "AGE" | "CPU" => Constraint::Length(6),
                "UNHEALTHY" | "MISSING" | "OPERATION" => Constraint::Length(9),
                "MEMORY" => Constraint::Length(if app.tab == 2 { 19 } else { 9 }),
                "RX / TX" => Constraint::Length(19),
                "SIZE" => Constraint::Length(9),
                "ID" => Constraint::Length(16),
                "DRIVER" | "SCOPE" | "PORT" => Constraint::Length(9),
                _ => Constraint::Fill(1),
            })
            .collect();
        let rendered = rows.into_iter().map(|r| {
            Row::new(
                columns
                    .iter()
                    .map(|&i| {
                        let value = r.cells.get(i).map(String::as_str).unwrap_or_default();
                        let color = match headers[i] {
                            "CONNECTION" | "STATE" | "STATUS" => status_color(value),
                            "STACK" | "HOST" => ACCENT,
                            _ => Color::White,
                        };
                        Cell::from(engine::clean(value)).style(Style::default().fg(color))
                    })
                    .collect::<Vec<_>>(),
            )
        });
        let title = format!(
            " {} [{count}] / {total} · {} {}{}{} ",
            TABS[app.tab],
            if app.tab == 7 {
                if app.descending {
                    "oldest received"
                } else {
                    "newest received"
                }
            } else {
                ["name", "stack", "state"][app.sort]
            },
            if app.descending { "↓" } else { "↑" },
            app.service
                .as_ref()
                .map_or(String::new(), |(h, s)| format!(" · {h}/{s}")),
            if app.filter.is_empty() {
                String::new()
            } else {
                format!(" · / {}", app.filter)
            }
        );
        let table = Table::new(rendered, widths)
            .header(
                Row::new(columns.iter().map(|&i| headers[i]).collect::<Vec<_>>())
                    .style(Style::default().fg(ACCENT).bold()),
            )
            .row_highlight_style(Style::default().bg(Color::DarkGray).bold())
            .highlight_symbol("▸ ")
            .block(
                Block::default()
                    .borders(Borders::TOP)
                    .title(title)
                    .border_style(Style::default().fg(ACCENT)),
            );
        let mut selection = TableState::default().with_selected(Some(app.selected));
        frame.render_stateful_widget(table, body, &mut selection);
        if count == 0 && body.height > 2 {
            frame.render_widget(
                Paragraph::new("No matching resources · / filter · b stack scope")
                    .style(Style::default().fg(MUTED)),
                Rect::new(body.x + 1, body.y + 2, body.width.saturating_sub(2), 1),
            );
        }
    }
    let keys = if app.detail.is_some() {
        " Esc back  / search  f follow  ? help  q quit"
    } else if area.width < 80 {
        " b scope  / filter  o sort  Enter open  ? help"
    } else {
        " b scope  [ ] cycle  / filter  o sort  O reverse  p details  Enter open  a actions  ? help  q quit"
    };
    let footer = if let Some(mode) = app.input {
        format!("{mode}{}", app.buffer)
    } else if notice.is_empty() {
        keys.into()
    } else {
        format!("{}\n{keys}", engine::clean(notice))
    };
    frame.render_widget(
        Paragraph::new(footer).style(Style::default().fg(Color::Gray)),
        layout[3],
    );
    if app.input == Some('@') {
        let text = format!(
            "Search: {}\n↑/↓ select · Enter apply · Esc cancel\nSubset: :ctx nexus,stargate\n\n{}",
            app.buffer,
            app.picker_options()
                .iter()
                .enumerate()
                .map(|(i, h)| format!(
                    "{} {}",
                    if i == app.picker_index { "▸" } else { " " },
                    if h == "all" { "ALL STACKS" } else { h }
                ))
                .collect::<Vec<_>>()
                .join("\n")
        );
        popup(
            frame,
            " Stack scope ",
            &text,
            app.picker_index.saturating_sub(10),
        );
    }
    if app.help {
        let overrides = app
            .repo
            .keys
            .iter()
            .map(|(k, v)| format!("  {k}: {v}"))
            .collect::<Vec<_>>()
            .join("\n");
        popup(
            frame,
            " Keybindings ",
            &format!("{HELP}\n\nCONFIGURED OVERRIDES\n{overrides}"),
            app.scroll,
        );
    }
    if let Some(index) = app.menu {
        let text = action_menu()
            .iter()
            .enumerate()
            .map(|(i, a)| format!("{} {a}", if i == index { "▸" } else { " " }))
            .collect::<Vec<_>>()
            .join("\n");
        popup(frame, " Actions · Enter select · Esc cancel ", &text, 0);
    }
    if let Some(p) = &app.pending {
        let text = format!(
            "{} on {}\nServices: {}\n{}\n\nType {} and Enter to confirm.\nEsc cancels.\n\n{}",
            p.action,
            p.host,
            if p.services.is_empty() {
                "all configured services".into()
            } else {
                p.services.join(", ")
            },
            p.extra.join(" "),
            p.token,
            app.buffer
        );
        popup(frame, " Confirm target ", &text, 0);
    }
}
fn popup(frame: &mut Frame, title: &str, text: &str, scroll: usize) {
    let area = frame.area();
    let width = area.width.saturating_sub(6).min(88);
    let height = area.height.saturating_sub(4).min(42);
    let rect = Rect::new(
        (area.width - width) / 2,
        (area.height - height) / 2,
        width,
        height,
    );
    frame.render_widget(Clear, rect);
    frame.render_widget(
        Paragraph::new(text)
            .scroll((scroll.min(u16::MAX as usize) as u16, 0))
            .block(
                Block::bordered()
                    .title(title)
                    .border_style(Style::default().fg(ACCENT)),
            ),
        rect,
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fleet() -> App {
        let repo = Repo::load(Some(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .parent()
                .unwrap(),
        ))
        .unwrap();
        let mut app = App::new(repo);
        for (host, state) in &mut app.states {
            state.seen = Some(Instant::now());
            state.desired = vec!["app".into()];
            state.snapshot.containers = vec![Container {
                id: "same-id".into(),
                name: "app-1".into(),
                service: "app".into(),
                project: host.clone(),
                image: "app:latest".into(),
                state: "running".into(),
                status: "Up 1 hour (healthy)".into(),
                ports: vec![Port {
                    private: 80,
                    public: Some(8080),
                    ip: "0.0.0.0".into(),
                    protocol: "tcp".into(),
                }],
            }];
            state.stats.insert(
                "same-id".into(),
                (
                    Stats {
                        cpu: 2.5,
                        memory: 1048576,
                        limit: 2097152,
                        rx: 1024,
                        tx: 2048,
                    },
                    Instant::now(),
                ),
            );
        }
        app
    }

    #[test]
    fn unified_rows_keep_actions_on_the_selected_stack() {
        let mut app = fleet();
        assert_eq!(
            app.rows()
                .iter()
                .map(|r| r.key.as_str())
                .collect::<Vec<_>>(),
            ["mothership/app", "nexus/app", "stargate/app"]
        );
        app.selected = 2;
        app.request("restart", vec![]);
        let pending = app.pending.take().unwrap();
        assert_eq!(
            (&*pending.host, pending.services, &*pending.token),
            ("stargate", vec!["app".to_string()], "stargate")
        );
        app.states.get_mut("stargate").unwrap().seen =
            Some(Instant::now() - Duration::from_secs(31));
        app.request("restart", vec![]);
        assert!(app.pending.is_none());
        assert_eq!(app.notice, "Target is offline or stale");
        app.select_tab(6);
        app.filter = "stack:nexus".into();
        app.states.get_mut("nexus").unwrap().snapshot.containers[0].project = "unmanaged".into();
        app.request("stop", vec![]);
        assert!(app.pending.is_none());
        assert_eq!(
            app.notice,
            "Select a managed service, or use Stacks for a whole-stack operation"
        );
    }

    #[test]
    fn scope_filters_sorting_and_drilldown_restore_the_exact_view() {
        let mut app = fleet();
        let (active, rx) = watch::channel(Vec::new());
        app.states.get_mut("nexus").unwrap().snapshot.containers[0].status =
            "Up 1 hour (unhealthy)".into();
        assert_eq!(app.rows()[0].host, "nexus");
        app.filter = "stack:nexus,stargate state:unhealthy ap".into();
        assert_eq!(
            app.rows()
                .iter()
                .map(|r| r.key.as_str())
                .collect::<Vec<_>>(),
            ["nexus/app"]
        );
        app.stabilize();
        app.drill_down(&active);
        assert_eq!(app.tab, 2);
        assert_eq!(&*rx.borrow(), &["nexus"]);
        assert_eq!(
            app.rows()
                .iter()
                .map(|r| r.key.as_str())
                .collect::<Vec<_>>(),
            ["nexus/same-id"]
        );
        app.back(&active);
        assert_eq!(app.tab, 1);
        assert!(app.scope.is_empty());
        assert_eq!(app.filter, "stack:nexus,stargate state:unhealthy ap");
        assert_eq!(app.selected_row().unwrap().key, "nexus/app");
        app.filter.clear();
        app.cycle_scope(false, &active);
        assert_eq!(app.scope, ["stargate"]);
        app.cycle_scope(true, &active);
        assert!(app.scope.is_empty());
        app.select_scope(vec!["mothership".into(), "stargate".into()], &active);
        app.select_tab(2);
        assert_eq!(
            app.rows()
                .iter()
                .map(|r| r.key.as_str())
                .collect::<Vec<_>>(),
            ["mothership/same-id", "stargate/same-id"]
        );
    }

    #[tokio::test]
    async fn log_search_keeps_its_target_and_restores_selection() {
        let mut app = fleet();
        // Detail tasks may only reach this deliberately absent local socket.
        for host in app.repo.hosts.values_mut() {
            host.endpoint = "unix:///tmp/tctl-absent-ui-test.sock".into();
        }
        let (tx, _) = mpsc::channel(16);
        app.filter = "app".into();
        app.selected = 2;
        app.stabilize();
        app.open_detail(true, &tx);
        app.filter = "no matching log line".into();
        app.stabilize();
        app.request("restart", vec![]);
        assert_eq!(app.pending.as_ref().unwrap().host, "stargate");
        app.close_detail();
        assert_eq!(app.filter, "app");
        assert_eq!(app.selected_row().unwrap().key, "stargate/app");
        assert_eq!(app.selected, 2);
    }

    #[test]
    fn context_commands_validate_subsets_and_forwarding_uses_row_host() {
        let mut app = fleet();
        let (tx, _) = mpsc::channel(16);
        let (active, rx) = watch::channel(Vec::new());
        let (refresh, _) = watch::channel(0);
        command(&mut app, "ctx nx,sg,nx", &tx, &active, &refresh);
        assert_eq!(app.scope, ["nexus", "stargate"]);
        assert_eq!(&*rx.borrow(), &["nexus", "stargate"]);
        command(&mut app, "ctx nexus,unknown", &tx, &active, &refresh);
        assert_eq!(app.scope, ["nexus", "stargate"]);
        command(&mut app, "ctx all", &tx, &active, &refresh);
        assert!(app.scope.is_empty());
        app.select_tab(0);
        app.selected = 1;
        app.drill_down(&active);
        assert_eq!(app.scope, ["nexus"]);
        app.back(&active);
        assert_eq!(app.selected_row().unwrap().host, "nexus");
        assert!(app.scope.is_empty());
        app.select_tab(6);
        let row = app
            .rows()
            .into_iter()
            .find(|r| r.host == "stargate")
            .unwrap();
        app.repo.hosts.get_mut("stargate").unwrap().endpoint = "unix:///tmp/absent.sock".into();
        start_forward(&mut app, 8080, row, &tx);
        assert_eq!(app.notice, "SSH forwarding requires an ssh:// target");
    }

    #[test]
    fn metrics_require_fresh_samples_and_events_merge_across_hosts() {
        let mut app = fleet();
        assert_eq!(&app.rows()[0].cells[4..6], ["2.5%", "1.0MiB"]);
        app.states
            .get_mut("mothership")
            .unwrap()
            .stats
            .get_mut("same-id")
            .unwrap()
            .1 = Instant::now() - Duration::from_secs(11);
        assert_eq!(&app.rows()[0].cells[4..6], ["—", "—"]);
        let (tx, _) = mpsc::channel(16);
        let (refresh, _) = watch::channel(0);
        handle_update(
            &mut app,
            Update::Event("stargate".into(), "12:01:00Z start app".into()),
            &tx,
            &refresh,
        );
        handle_update(
            &mut app,
            Update::Event("mothership".into(), "12:01:01Z stop app".into()),
            &tx,
            &refresh,
        );
        app.select_tab(7);
        assert_eq!(
            app.rows()
                .iter()
                .map(|r| (&*r.host, &*r.cells[1]))
                .collect::<Vec<_>>(),
            [
                ("mothership", "12:01:01Z stop app"),
                ("stargate", "12:01:00Z start app")
            ]
        );
    }

    fn render(app: &App, width: u16, height: u16) -> Vec<String> {
        let mut terminal =
            Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
        terminal.draw(|frame| draw(frame, app)).unwrap();
        terminal
            .backend()
            .buffer()
            .content
            .chunks(width as usize)
            .map(|row| row.iter().map(|cell| cell.symbol()).collect())
            .collect()
    }

    #[test]
    fn compact_layout_reserves_rows_for_data_and_keeps_targets_visible() {
        let app = fleet();
        let screen = render(&app, 120, 21);
        assert!(screen[0].contains("ALL STACKS"), "{}", screen.join("\n"));
        assert!(screen[3].contains("STACK") && screen[3].contains("CONNECTION"));
        assert!(screen[4].contains("mothership") && screen[4].contains("app"));
        assert!(screen[6].contains("stargate"));
        assert!(screen[20].contains("b scope"));
        let narrow = render(&app, 45, 12);
        assert!(
            narrow[4].contains("mothership")
                && narrow[4].contains("app")
                && narrow[4].contains("LIVE"),
            "{}",
            narrow.join("\n")
        );
        let tall = render(&app, 120, 30);
        assert!(tall[25].contains("p hide details"));
    }

    #[test]
    fn fuzzy_subsequence_ranks_closer_matches() {
        assert!(fuzzy_score("grafana", "gfn").is_some());
        assert!(fuzzy_score("postgres", "gfn").is_none());
        assert!(fuzzy_score("grafana", "gra") < fuzzy_score("my-grafana", "gra"));
    }
    #[test]
    fn vim_navigation_and_stale_display() {
        let repo = Repo::load(Some(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .parent()
                .unwrap(),
        ))
        .unwrap();
        let mut app = App::new(repo);
        assert_eq!(binding("k"), "up");
        assert_eq!(binding("l"), "next-tab");
        assert_eq!(binding("x"), "remove");
        app.states.get_mut("nexus").unwrap().seen = Some(Instant::now() - Duration::from_secs(31));
        assert_eq!(app.states["nexus"].label(), "STALE");
        let backend = ratatui::backend::TestBackend::new(120, 35);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|f| draw(f, &app)).unwrap();
        let rendered = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect::<String>();
        assert!(rendered.contains("tctl"));
        assert!(rendered.contains("STALE"));
        assert!(rendered.contains("Services"));
        app.select_tab(2);
        for i in 0..6000 {
            app.append_log(format!("line {i}"));
        }
        assert_eq!(app.lines.len(), 5000);
        assert_eq!(app.dropped, 1000);
        assert_eq!(app.lines.front().unwrap(), "line 1000");
    }
    #[test]
    fn confirmations_capture_selected_host_and_unmanaged_views_cannot_deploy_stack() {
        let repo = Repo::load(Some(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .parent()
                .unwrap(),
        ))
        .unwrap();
        let mut app = App::new(repo);
        app.select_tab(0);
        app.sort = 0;
        app.selected = 1;
        app.states.get_mut("nexus").unwrap().seen = Some(Instant::now());
        app.request("deploy", vec![]);
        assert_eq!(app.pending.as_ref().unwrap().host, "nexus");
        app.selected = 0;
        assert_eq!(app.pending.as_ref().unwrap().host, "nexus");
        app.pending = None;
        app.scope = vec!["nexus".into()];
        app.select_tab(3);
        app.request("deploy", vec![]);
        assert!(app.pending.is_none());
        app.logs = true;
        app.detail = Some(String::new());
        for i in 0..40 {
            app.append_log(format!("line {i}"));
        }
        app.move_by(-1);
        assert_eq!(app.scroll, 23);
        assert!(!app.follow);
    }
}
