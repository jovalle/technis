use anyhow::{Context, Result, bail, ensure};
use serde::Deserialize;
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Host {
    pub endpoint: String,
    pub expected_name: String,
    #[serde(default = "socket")]
    pub socket: String,
    #[serde(default)]
    pub vault_items: Vec<String>,
}
fn socket() -> String {
    "/var/run/docker.sock".into()
}

impl Host {
    pub fn ssh(&self) -> Option<&str> {
        self.endpoint.strip_prefix("ssh://")
    }
    pub fn validate(&self) -> Result<()> {
        ensure!(!self.expected_name.is_empty(), "expected_name must be set");
        if let Some(host) = self.ssh() {
            ensure!(
                !host.is_empty()
                    && !host.starts_with('-')
                    && host
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"._-@".contains(&b)),
                "SSH endpoint must be an SSH alias, optionally user@alias; configure ports in ~/.ssh/config"
            );
            safe_path(&self.socket)?;
        } else {
            ensure!(
                self.endpoint.starts_with("unix:///"),
                "Only explicit ssh:// and unix:/// endpoints are supported"
            );
        }
        Ok(())
    }
}

pub fn safe_name(name: &str) -> Result<()> {
    ensure!(
        name.as_bytes()
            .first()
            .is_some_and(u8::is_ascii_alphanumeric)
            && name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b)),
        "Invalid resource name: {name}"
    );
    Ok(())
}
pub fn safe_path(path: &str) -> Result<()> {
    ensure!(
        path.starts_with('/')
            && !path.split('/').any(|p| p == "..")
            && path
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"/._-".contains(&b)),
        "Unsafe remote path"
    );
    Ok(())
}

#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct Settings {
    #[serde(default)]
    hosts: BTreeMap<String, Host>,
    #[serde(default)]
    keys: BTreeMap<String, String>,
}

#[derive(Clone)]
pub struct Repo {
    pub root: PathBuf,
    pub hosts: BTreeMap<String, Host>,
    pub keys: BTreeMap<String, String>,
    pub environment: BTreeMap<String, String>,
}
impl Repo {
    pub fn load(root: Option<&Path>) -> Result<Self> {
        let root = match root {
            Some(path) => path.canonicalize()?,
            None => {
                let cwd = std::env::current_dir()?;
                let exe = std::env::current_exe()?;
                cwd.ancestors()
                    .chain(exe.ancestors())
                    .find(|p| p.join("docker/stacks").is_dir())
                    .context("Cannot find repository; pass --root /path/to/technis")?
                    .to_path_buf()
            }
        };
        ensure!(
            root.join("docker/stacks").is_dir(),
            "Repository has no docker/stacks directory"
        );
        let path = root.join("tctl.toml");
        let mut settings: Settings = if path.exists() {
            toml::from_str(&std::fs::read_to_string(path)?)?
        } else {
            Settings::default()
        };
        for entry in std::fs::read_dir(root.join("docker/stacks"))? {
            let entry = entry?;
            if !entry.path().join("compose.yaml").is_file() {
                continue;
            }
            let name = entry.file_name().to_string_lossy().to_string();
            safe_name(&name)?;
            settings.hosts.entry(name.clone()).or_insert(Host {
                endpoint: format!("ssh://{name}"),
                expected_name: name,
                socket: socket(),
                vault_items: Vec::new(),
            });
        }
        ensure!(!settings.hosts.is_empty(), "No stacks found");
        for (name, host) in &settings.hosts {
            safe_name(name)?;
            host.validate()?;
        }
        Ok(Self {
            root,
            hosts: settings.hosts,
            keys: settings.keys,
            environment: BTreeMap::new(),
        })
    }
    pub fn stack_dir(&self, name: &str) -> PathBuf {
        self.root.join("docker/stacks").join(name)
    }
    pub fn host(&self, name: &str) -> Result<&Host> {
        self.hosts
            .get(name)
            .with_context(|| format!("Unknown stack: {name}"))
    }
    pub fn normalize<'a>(&self, name: &'a str) -> &'a str {
        match name {
            "nx" => "nexus",
            "ms" => "mothership",
            "sg" => "stargate",
            _ => name,
        }
    }
    pub fn env_files(&self, name: &str) -> [PathBuf; 2] {
        [
            self.root.join("docker/.env"),
            self.stack_dir(name).join(".env"),
        ]
    }
    pub fn check_env(&self, name: &str) -> Result<()> {
        for path in self.env_files(name) {
            if !path.is_file() {
                bail!(
                    "Missing {}. Run tctl init, fill settings, and inject secrets before Compose actions.",
                    path.display()
                );
            }
        }
        Ok(())
    }
    pub fn init(&self) -> Result<()> {
        use std::{fs::OpenOptions, io::Write, os::unix::fs::OpenOptionsExt};
        for path in std::iter::once(self.root.join("docker/.env"))
            .chain(self.hosts.keys().map(|s| self.stack_dir(s).join(".env")))
        {
            if path.exists() {
                continue;
            }
            let template = path.with_file_name(".env.example");
            let content = std::fs::read(&template)
                .with_context(|| format!("Missing {}", template.display()))?;
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&path)?;
            file.write_all(&content)?;
            println!(
                "Created {}. Fill required values before deploying.",
                path.display()
            );
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reject_shell_and_path_injection() {
        for name in ["--help", "a;id", "a b", "../x", ""] {
            assert!(safe_name(name).is_err(), "{name}");
        }
        for path in ["/tmp/../etc", "/tmp/a'", "relative"] {
            assert!(safe_path(path).is_err());
        }
        safe_path("/srv/technis/data").unwrap();
        let host = Host {
            endpoint: "ssh://-oProxyCommand=id".into(),
            expected_name: "nexus".into(),
            socket: socket(),
            vault_items: Vec::new(),
        };
        assert!(host.validate().is_err());
    }
    #[test]
    fn clean_clone_discovers_stacks_and_init_preserves_files() {
        let temp = tempfile::tempdir().unwrap();
        let dir = temp.path().join("docker/stacks/nexus");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("compose.yaml"), "name: nexus").unwrap();
        std::fs::write(dir.join(".env.example"), "STACK_DATA_ROOT=''\n").unwrap();
        std::fs::write(temp.path().join("docker/.env.example"), "TZ=UTC\n").unwrap();
        let repo = Repo::load(Some(temp.path())).unwrap();
        assert_eq!(repo.host("nexus").unwrap().endpoint, "ssh://nexus");
        assert!(repo.check_env("nexus").is_err());
        repo.init().unwrap();
        std::fs::write(dir.join(".env"), "keep").unwrap();
        repo.init().unwrap();
        assert_eq!(std::fs::read_to_string(dir.join(".env")).unwrap(), "keep");
    }
}
