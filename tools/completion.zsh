# Zsh tab completion for bin/vault and the arguments of this repository's just recipes.
# Source it from ~/.zshrc after compinit; `just prepare workstation` checks for it.

(($+functions[compdef])) || return 0

typeset -g _technis_root=${${(%):-%x}:A:h:h}
# Re-sourcing must not capture these wrappers as their own fallbacks.
[[ $_comps[just] == _technis_just ]] || typeset -g _technis_just_fallback=${_comps[just]:-_default}
[[ $_comps[vault] == _technis_vault_command ]] || typeset -g _technis_vault_fallback=${_comps[vault]:-_default}

_technis_stack_names() {
  compadd "$@" -- $_technis_root/docker/stacks/*(N/:t)
}

# Compose files of the given stacks: each stack's own file and every file it includes.
_technis_compose_files() {
  local stack dir file
  reply=()
  for stack; do
    dir=$_technis_root/docker/stacks/$stack
    [[ -f $dir/compose.yaml ]] || continue
    reply+=($dir/compose.yaml)
    for file in ${(f)"$(sed -nE 's#^[[:space:]-]*(path:)?[[:space:]]*([^[:space:]]+/compose\.yaml)[[:space:]]*$#\2#p' $dir/compose.yaml)"}; do
      [[ -f $dir/$file ]] && reply+=($dir/$file)
    done
  done
}

_technis_services() {
  _technis_compose_files "$@"
  (($#reply)) || return 1
  reply=(${(f)"$(awk 'FNR == 1 { s = 0 } /^services:/ { s = 1; next } /^[^[:space:]#]/ { s = 0 }
    s && /^  [A-Za-z0-9][A-Za-z0-9_.-]*:/ { sub(/^ +/, ""); sub(/:.*/, ""); print }' $reply | sort -u)"})
}

# A stack, a service, or stack/service, as docker/scripts/stacks resolves a target.
_technis_target() {
  local -a expl stacks services checks
  if [[ $PREFIX == */* ]]; then
    _technis_services ${PREFIX%%/*}
    compset -P '*/'
    _wanted services expl service compadd -a reply
    return
  fi
  stacks=($_technis_root/docker/stacks/*(N/:t))
  _technis_services $stacks
  services=($reply)
  # _arguments inserts its compadd options before this word.
  [[ ${@[-1]} == check ]] && checks=(workstation privileges)
  _alternative \
    ${checks:+'checks:check:compadd -a checks'} \
    'stacks:stack:compadd -S " " -r / -a stacks' \
    'services:service:compadd -a services'
}

# Service directories that `just sync STACK` copies.
_technis_includes() {
  compadd "$@" -- ${(f)"$(sed -nE 's#.*\.\./\.\./services/(.+)/compose\.yaml.*#\1#p' \
    $_technis_root/docker/stacks/$line[1]/compose.yaml 2>/dev/null | sort -u)"}
}

# Hosts in the Ansible amt, ipmi, and glkvm groups, as console/server.mjs selects them.
_technis_hosts() {
  compadd "$@" -- ${(f)"$(cd $_technis_root && ansible-inventory --list 2>/dev/null | python3 -c '
import json, sys
inventory = json.load(sys.stdin)
def hosts(group, seen):
    if group in seen:
        return set()
    seen.add(group)
    node = inventory.get(group, {})
    return set(node.get("hosts", [])).union(*(hosts(child, seen) for child in node.get("children", [])))
print("\n".join(sorted(set().union(*(hosts(group, set()) for group in ("amt", "ipmi", "glkvm"))))))
' 2>/dev/null)"}
}

# Folder and key names come from `vault list`; values never leave Infisical.
_technis_vault_names() {
  local folders_only=$1 dir= ret=1
  shift
  [[ $PREFIX == */* ]] && dir=${PREFIX%/*}
  local -a run=($_technis_root/bin/vault list -- "$dir")
  # A rate-limited request retries for up to a minute; a completion should not wait that long.
  (($+commands[timeout])) && run=(timeout 10 $run)
  local -a names=(${(f)"$("${run[@]}" 2>/dev/null </dev/null)"})
  local -a folders=(${${(M)names:#*/}%/}) keys=(${names:#*/})
  compset -P '*/'
  # Paths are KEY, ITEM/FIELD, or ITEM/SECTION/FIELD; list and show take any folder.
  if ((folders_only)) || [[ $dir != */* ]]; then
    compadd "$@" -q -S / -a folders && ret=0
  fi
  if ((! folders_only)) && [[ -n $dir ]]; then
    compadd "$@" -a keys && ret=0
  fi
  return ret
}
_technis_vault_path() { _technis_vault_names 0 "$@"; }
_technis_vault_folder() { _technis_vault_names 1 "$@"; }

_technis_vault() {
  local curcontext=$curcontext state line ret=1
  local -a subcommands=(
    'set:store a value, asking before overwriting'
    'get:print one value'
    'list:list folders and keys, never values'
    'remove:delete one value after confirming'
    'login:log in to Infisical Cloud with the infisical CLI'
    "show:print one folder's values as JSON"
    'inject:resolve vault references from stdin to stdout'
  )
  _arguments -C '(- : *)'{-h,--help}'[show help]' '1:command:->command' '*::argument:->argument' && ret=0
  case $state in
    command) _describe -t commands 'vault command' subcommands && ret=0 ;;
    argument)
      case $words[1] in
        set) _arguments '(-e -p)-e[write the value in an editor]' '(-e -p)-p[hidden input, entered twice]' \
          '1:path:_technis_vault_path' && ret=0 ;;
        get | remove) _arguments '1:path:_technis_vault_path' && ret=0 ;;
        list | show) _arguments '1:folder:_technis_vault_folder' && ret=0 ;;
        *) _message 'no more arguments' ;;
      esac
      ;;
  esac
  return ret
}

# Another tool named vault, such as HashiCorp Vault, keeps its own completion.
_technis_vault_command() {
  local cmd=$words[1]
  [[ $cmd == */* ]] || cmd=$commands[$cmd]
  if [[ -n $cmd && ${cmd:A} == $_technis_root/bin/vault ]]; then
    _technis_vault
  else
    eval "$_technis_vault_fallback"
  fi
}

# words[1] is the recipe name.
_technis_recipe() {
  local -a expl stack_options=(
    '*'{-s,--stack}'[limit to stacks]:stack:_sequence -s , _technis_stack_names'
    '(-y --yes)'{-y,--yes}'[skip the confirmation for several stacks]'
  )
  case $words[1] in
    deploy | status | update) _arguments -s $stack_options '1:target:_technis_target' ;;
    check) _arguments -s $stack_options '1:target:_technis_target check' ;;
    logs)
      _arguments -s $stack_options \
        '(-f --follow)'{-f,--follow}'[follow log output]' \
        '(-n --tail)'{-n,--tail}'[number of lines from the end]:lines' \
        '--since[show logs since a timestamp or relative time]:time' \
        '--until[show logs before a timestamp or relative time]:time' \
        '(-t --timestamps)'{-t,--timestamps}'[show timestamps]' \
        '1:target:_technis_target'
      ;;
    sync) _arguments '1:stack:_technis_stack_names' '-n[preview without copying]' '*:service:_technis_includes' ;;
    compose)
      if ((CURRENT == 2)); then
        _wanted stacks expl stack _technis_stack_names
      else
        words=(docker compose "${(@)words[3,-1]}")
        _normal
      fi
      ;;
    vault) _technis_vault ;;
    prepare) _arguments '1:target:((workstation\:"install tools and configure the workstation" contexts\:"create the Docker contexts"))' ;;
    open) _arguments '1:tool:(console)' '2:host:_technis_hosts' ;;
    *) _message 'no arguments' ;;
  esac
}

# just completes recipe names only, so recipe arguments are added here for this repository's justfile.
_technis_just() {
  local dir=$PWD ret=1
  until [[ -f $dir/justfile || -f $dir/Justfile || -f $dir/.justfile || $dir == / ]]; do dir=${dir:h}; done
  if ((CURRENT > 2)) && [[ ${dir:A} == $_technis_root && $words[2] != -* ]]; then
    words=("${(@)words[2,-1]}")
    ((CURRENT--))
    _technis_recipe
    return
  fi
  if (($+functions[_clap_dynamic_completer_just])); then
    _clap_dynamic_completer_just && ret=0
  else
    eval "$_technis_just_fallback" && ret=0
  fi
  # just's completion script registers itself on first use; keep this wrapper in front.
  compdef _technis_just just
  return ret
}

compdef _technis_vault_command vault
compdef _technis_just just
