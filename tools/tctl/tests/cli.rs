use std::{
    fs,
    io::{Read, Write},
    os::unix::{fs::PermissionsExt, net::UnixListener},
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::Duration,
};

struct Daemon {
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}
impl Drop for Daemon {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        self.thread.take().unwrap().join().unwrap();
    }
}
fn daemon(path: &std::path::Path, name: &str) -> Daemon {
    let listener = UnixListener::bind(path).unwrap();
    listener.set_nonblocking(true).unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let done = stop.clone();
    let name = name.to_string();
    let thread = thread::spawn(move || {
        while !done.load(Ordering::SeqCst) {
            match listener.accept() {
                Ok((mut stream, _)) => {
                    stream
                        .set_read_timeout(Some(Duration::from_secs(2)))
                        .unwrap();
                    let mut request = Vec::new();
                    let mut byte = [0u8; 1];
                    while stream.read(&mut byte).unwrap_or(0) > 0 {
                        request.push(byte[0]);
                        if request.ends_with(b"\r\n\r\n") {
                            break;
                        }
                    }
                    let request = String::from_utf8_lossy(&request);
                    let url = request.split_whitespace().nth(1).unwrap_or("");
                    let body = if url.ends_with("/version") {
                        r#"{"ApiVersion":"1.53","MinAPIVersion":"1.24"}"#.to_string()
                    } else if url.ends_with("/info") {
                        format!(r#"{{"Name":"{name}"}}"#)
                    } else if url.contains("/containers/json") {
                        format!(
                            r#"[{{"Id":"abc123","Names":["/app-1"],"Image":"app:test","State":"running","Status":"Up 1 minute","Labels":{{"com.docker.compose.project":"{name}","com.docker.compose.service":"app"}}}}]"#
                        )
                    } else if url.contains("/volumes") {
                        r#"{"Volumes":[],"Warnings":[]}"#.into()
                    } else {
                        "[]".into()
                    };
                    let response = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    let _ = stream.write_all(response.as_bytes());
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(5))
                }
                Err(e) => panic!("{e}"),
            }
        }
    });
    Daemon {
        stop,
        thread: Some(thread),
    }
}

#[test]
fn real_cli_reads_bollard_api_and_refuses_wrong_host_or_unconfirmed_mutation() {
    let temp = tempfile::Builder::new()
        .prefix("tctl-test-")
        .tempdir_in("/tmp")
        .unwrap();
    let root = temp.path();
    let stacks = root.join("docker/stacks");
    let tools = root.join("tools");
    fs::create_dir_all(&tools).unwrap();
    let mut settings = String::new();
    let mut daemons = Vec::new();
    for name in ["mothership", "nexus", "stargate"] {
        let dir = stacks.join(name);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("compose.yaml"), "name: test\n").unwrap();
        fs::write(dir.join(".env"), "").unwrap();
        let socket = root.join(format!("{name}.sock"));
        daemons.push(daemon(&socket, name));
        settings.push_str(&format!(
            "[hosts.{name}]\nendpoint = 'unix://{}'\nexpected_name = '{name}'\n",
            socket.display()
        ));
    }
    fs::write(root.join("tctl.toml"), &settings).unwrap();
    fs::write(root.join("docker/.env"), "").unwrap();
    let docker = tools.join("docker");
    fs::write(&docker,r#"#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['TCTL_TEST_EVENTS'], 'a') as out: out.write(json.dumps(args) + '\n')
if '--services' in args: print('app')
if '--environment' in args: print('STACK_DATA_ROOT=/tmp/tctl-test-data')
if '--format' in args: print(json.dumps({'services': {'app': {'image': 'app:test', 'environment': {'TOKEN': 'never-print'}, 'command': ['never-print']}}}))
if 'up' in args and args[args.index('--project-name') + 1] == os.environ.get('TCTL_TEST_FAIL'): sys.exit(7)
"#).unwrap();
    fs::set_permissions(&docker, fs::Permissions::from_mode(0o755)).unwrap();
    let events = root.join("events");
    let run = |args: &[&str], failure: &str| {
        Command::new(env!("CARGO_BIN_EXE_tctl"))
            .arg("--root")
            .arg(root)
            .args(args)
            .env(
                "PATH",
                format!("{}:{}", tools.display(), std::env::var("PATH").unwrap()),
            )
            .env("DOCKER_CONTEXT", "unrelated")
            .env("DOCKER_HOST", "unix:///wrong.sock")
            .env("TCTL_TEST_EVENTS", &events)
            .env("TCTL_TEST_FAIL", failure)
            .output()
            .unwrap()
    };
    let status = run(&["status", "nexus"], "");
    assert!(
        status.status.success(),
        "{}",
        String::from_utf8_lossy(&status.stderr)
    );
    assert!(String::from_utf8_lossy(&status.stdout).contains("app-1\tapp\trunning"));
    let denied = run(&["restart", "nexus", "app"], "");
    assert!(!denied.status.success());
    assert!(String::from_utf8_lossy(&denied.stderr).contains("--yes"));
    assert!(!events.exists());
    fs::remove_file(root.join("docker/.env")).unwrap();
    let start = run(&["--yes", "start", "nexus", "app"], "");
    assert!(
        start.status.success(),
        "{}",
        String::from_utf8_lossy(&start.stderr)
    );
    assert!(String::from_utf8_lossy(&start.stdout).contains("start app-1"));
    assert!(
        !events.exists(),
        "Lifecycle operations must use Bollard without Compose"
    );
    fs::write(root.join("docker/.env"), "").unwrap();
    let config = run(&["config", "nexus", "app"], "");
    assert!(
        config.status.success(),
        "{}",
        String::from_utf8_lossy(&config.stderr)
    );
    let preview = String::from_utf8_lossy(&config.stdout);
    assert!(preview.contains("app:test"));
    assert!(!preview.contains("never-print"));
    fs::write(&events, "").unwrap();
    let deploy = run(&["--yes", "deploy", "app"], "nexus");
    assert!(!deploy.status.success());
    let events_text = fs::read_to_string(&events).unwrap();
    let calls: Vec<Vec<String>> = events_text
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    let deployed: Vec<_> = calls
        .iter()
        .filter(|a| a.iter().any(|s| s == "up"))
        .map(|a| a[a.iter().position(|s| s == "--project-name").unwrap() + 1].as_str())
        .collect();
    assert_eq!(
        deployed,
        ["mothership", "nexus"],
        "{}",
        String::from_utf8_lossy(&deploy.stderr)
    );
    assert!(!events_text.contains("stargate"));
    assert!(
        calls
            .iter()
            .filter(|a| a.iter().any(|s| s == "up"))
            .all(|a| a.contains(&"--no-deps".into()) && a.contains(&"--wait".into()))
    );
    fs::write(&events, "").unwrap();
    fs::write(
        root.join("tctl.toml"),
        settings.replace("expected_name = 'nexus'", "expected_name = 'wrong'"),
    )
    .unwrap();
    let wrong = run(&["--yes", "restart", "nexus", "app"], "");
    assert!(!wrong.status.success());
    assert!(
        String::from_utf8_lossy(&wrong.stderr).contains("Wrong daemon"),
        "{}",
        String::from_utf8_lossy(&wrong.stderr)
    );
    assert_eq!(fs::read_to_string(&events).unwrap(), "");
    drop(daemons);
}
