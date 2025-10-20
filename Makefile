.PHONY: default deps submodules status commit

default: deps submodules status

deps:
	@command -v task >/dev/null 2>&1 || { \
		echo "Installing task..."; \
		sh -c "$$(curl -fsSL https://taskfile.dev/install.sh)" -- -d -b ~/.local/bin; \
	}
	@command -v ansible >/dev/null 2>&1 || { \
		echo "Installing ansible..."; \
		pip3 install --user ansible; \
	}
	@command -v terraform >/dev/null 2>&1 || { \
		echo "Installing terraform..."; \
		curl -fsSL https://releases.hashicorp.com/terraform/1.9.8/terraform_1.9.8_linux_amd64.zip -o /tmp/terraform.zip && \
		unzip -o /tmp/terraform.zip -d ~/.local/bin && \
		rm /tmp/terraform.zip; \
	}
	@command -v kubectl >/dev/null 2>&1 || { \
		echo "Installing kubectl..."; \
		curl -fsSL "https://dl.k8s.io/release/$$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" -o ~/.local/bin/kubectl && \
		chmod +x ~/.local/bin/kubectl; \
	}
	@command -v talosctl >/dev/null 2>&1 || { \
		echo "Installing talosctl..."; \
		curl -fsSL https://github.com/siderolabs/talos/releases/latest/download/talosctl-linux-amd64 -o ~/.local/bin/talosctl && \
		chmod +x ~/.local/bin/talosctl; \
	}
	@command -v flux >/dev/null 2>&1 || { \
		echo "Installing flux..."; \
		curl -fsSL https://fluxcd.io/install.sh | bash -s -- ~/.local/bin; \
	}
	@command -v sops >/dev/null 2>&1 || { \
		echo "Installing sops..."; \
		curl -fsSL https://github.com/getsops/sops/releases/download/v3.9.1/sops-v3.9.1.linux.amd64 -o ~/.local/bin/sops && \
		chmod +x ~/.local/bin/sops; \
	}
	@command -v age >/dev/null 2>&1 || { \
		echo "Installing age..."; \
		curl -fsSL https://github.com/FiloSottile/age/releases/download/v1.2.0/age-v1.2.0-linux-amd64.tar.gz -o /tmp/age.tar.gz && \
		tar -xzf /tmp/age.tar.gz -C /tmp && \
		mv /tmp/age/age ~/.local/bin/ && \
		mv /tmp/age/age-keygen ~/.local/bin/ && \
		rm -rf /tmp/age /tmp/age.tar.gz; \
	}
	@command -v helmfile >/dev/null 2>&1 || { \
		echo "Installing helmfile..."; \
		curl -fsSL https://github.com/helmfile/helmfile/releases/download/v0.169.1/helmfile_0.169.1_linux_amd64.tar.gz -o /tmp/helmfile.tar.gz && \
		tar -xzf /tmp/helmfile.tar.gz -C ~/.local/bin helmfile && \
		rm /tmp/helmfile.tar.gz; \
	}
	@command -v jq >/dev/null 2>&1 || { \
		echo "Installing jq..."; \
		curl -fsSL https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-amd64 -o ~/.local/bin/jq && \
		chmod +x ~/.local/bin/jq; \
	}
	@command -v envsubst >/dev/null 2>&1 || { \
		echo "envsubst is part of gettext package. Install with: apt install gettext-base (Debian/Ubuntu) or yum install gettext (RHEL/CentOS)"; \
	}
	@command -v mkdocs >/dev/null 2>&1 || { \
		echo "Installing mkdocs..."; \
		pip3 install --user mkdocs; \
	}
	@command -v cz >/dev/null 2>&1 || { \
		echo "Installing commitizen..."; \
		pip3 install --user commitizen; \
	}

submodules:
	git submodule update --init --recursive

status:
	@git submodule status

commit:
	@cz commit
