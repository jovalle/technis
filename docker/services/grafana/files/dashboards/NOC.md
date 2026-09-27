# Technis operations console

[Open the 1920×1080 kiosk view](https://grafana.techn.is/d/technis-noc?kiosk).

The console is a separate dashboard; it preserves the existing troubleshooting dashboards. All panels are native Grafana stat, table, time-series or text panels. No additional plugin, collector, alert rule or datasource is required by this dashboard change.

## Screen contract

The JSON uses 24 columns and 26 grid rows. At Grafana's 30 px row height with 8 px gaps, panels occupy about 980 px vertically, leaving room for kiosk padding inside a 1080 px viewport. There are no repeated panels or unbounded service lists. The target is a 1920×1080 browser content area at normal zoom, with Grafana kiosk mode and browser fullscreen enabled.

| Region                  | Grid rows | Content                                                                                                   |
| ----------------------- | --------- | --------------------------------------------------------------------------------------------------------- |
| Global status           | 0–2       | Worst state, firing alert counts, host/check coverage, storage, WAN, local time and metric age            |
| Domains and exceptions  | 3–11      | Nine domain groups; five highest-priority problems; five newest significant events                        |
| Hosts                   | 12–17     | Host state, CPU, memory, fullest filesystem, receive/transmit rate, temperature, uptime, container counts |
| Pressure trends         | 18–22     | CPU/memory/I/O PSI and the slowest HTTP checks over one hour                                              |
| Coverage and navigation | 23–25     | Unchecked containers, observed start changes/OOMs, state legend and detail links                          |

The attachment ended during Region 3. Regions 4 and 5 implement its stated goals for short-term trends and visible monitoring gaps.

## Read the states

- **OK**, muted green: a relevant observation passed.
- **UNKNOWN**, gray: no applicable evidence, unsupported sensor, missing probe or a running container without a health check.
- **WARN**, amber: starting container or a resource threshold exceeded.
- **FAIL**, red: stopped/unhealthy container, failed check, host unavailable or critical alert.

Domain state is the worst meaningful member state: FAIL > WARN > UNKNOWN > OK. The global state includes domains, critical/warning alerts, expected host coverage and telemetry age. Unknown backup/WAN coverage prevents a false all-clear, while never hiding a failure.

Container running state alone does not establish health. The header intentionally says **Passed / Seen**, not healthy/expected: there is no reliable desired-state container inventory in the current telemetry. Init containers are excluded through an explicit regex. HTTP checks use Gatus results, require a live Gatus scrape, and reject stale probe samples.

Header CRIT/WARN counts are firing **alert instances**. The problem list groups matching alert objects/reasons and also includes current stopped/unhealthy containers and resource pressure. Therefore the number of visible problem rows need not equal the header counts. Alert ages come from `ALERTS_FOR_STATE`; derived conditions display a dash when their age is unknown. Existing read-only-filesystem alerts on Nexus are displayed without suppression; review the underlying rule and intended read-only mounts separately.

Host warning thresholds are CPU/memory/filesystem usage above 90%, or CPU PSI above 20% averaged over five minutes. These are dashboard decisions, not changes to alert rules. Resource cells additionally use warning/critical colors at 80/92% utilization and 75/90°C. The storage domain warns at 85% filesystem usage. Tune these adapters to the operating policy; absolute temperature limits vary by sensor.

## Events and trends

Events use observed container start times, host boot times, OOM counter changes, and Docker health/running/probe transitions. VictoriaMetrics `tlast_change_over_time` supplies sampled transition timestamps. This panel is **not a complete Docker event journal**: it can miss multiple transitions between scrapes and only shows the newest change per series in the last hour. Start changes in the footer identify restarts within an existing cAdvisor series; container recreation can create a new series and is not an exact restart counter. No routine application logs are included.

Network columns show traffic in bytes/second, not percent of link capacity. The fullest filesystem is shown per host. Missing temperature on the virtual host stays UNKNOWN. Pressure charts explain saturation even when CPU utilization is below 100%.

## Adapt labels and membership

The generator is [`scripts/observability/noc.py`](../../../../../scripts/observability/noc.py). Its opening adapters and `DOMAINS` list own the site-specific queries.

| Adapter              | Current assumption                               | Change when                                                           |
| -------------------- | ------------------------------------------------ | --------------------------------------------------------------------- |
| `DS`                 | VictoriaMetrics datasource UID `victoriametrics` | Importing into a different Grafana instance                           |
| `S`, `N`             | `environment`, `host`, `stack`; node job `node`  | Exporters use `instance`, `node`, `compose_project` or different jobs |
| `DOMAINS`            | Container-name membership and Gatus probe names  | Adding services/domains or adopting a `category` label                |
| `expected_hosts`     | Three monitored hosts                            | Inventory changes                                                     |
| `ignored_containers` | Init/data-init containers are intentional jobs   | Other intentionally stopped services or jobs need exclusion           |
| `wan_probe`          | Gatus names `WAN` or `Internet`                  | Independent external connectivity probes become available             |
| BACKUPS              | Explicit UNKNOWN placeholders                    | A trustworthy last-success/failure/age metric is available            |

Environment, host and stack variables are hidden in kiosk mode to preserve space but remain URL-selectable (`var-host=...`, etc.). This overview defaults to the whole fleet; its named host tiles and expected-host total are an explicit three-host inventory. Update them together when adapting the fleet. Data links open the appropriate host drilldown.

Current `stack` labels identify the host-level stack. Separate nested Compose project/service labels are not exported consistently; the dashboard does not invent those relationships. Adapt the selectors/joins when `compose_project` and `service` labels become available. The hierarchy is global → domain → host → stack filter → service/container. The compact event list shows host/object; detailed dashboards retain the underlying labels.

Most queries are PromQL. Event timestamps and stable label sorting use VictoriaMetrics MetricsQL; replace those two functions when moving to plain Prometheus. Grafana refreshes every 15 seconds. The clock advances on dashboard refresh rather than using a separate clock plugin.

## Validation and files

- [`noc.json`](noc.json) is the importable Grafana dashboard.
- `rtk proxy python3 scripts/observability/noc.py` regenerates it locally.
- `rtk proxy python3 scripts/observability/test_noc.py` checks layout bounds, overlap, native panel types and list limits.
- Add `--live` to validate queries through the existing Nexus SSH alias and exercise health-state cases without writing synthetic metrics.
- The full dashboard was rendered in Waterfox's 1920×1080, DPR 1 viewport. Kiosk content, five event entries, host rows and the footer fit without scrolling. Browser zoom was not changed.
