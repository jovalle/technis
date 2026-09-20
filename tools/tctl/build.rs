fn main() {
    println!("cargo:rerun-if-env-changed=TCTL_REVISION");
    let revision = std::env::var("TCTL_REVISION").unwrap_or_else(|_| "local".into());
    assert!(
        revision
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-')
    );
    println!("cargo:rustc-env=TCTL_REVISION={revision}");
}
