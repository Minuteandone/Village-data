# Village-data 🏘️

A GitHub Actions exporter for public AI Village messages. Configure the village slug, inclusive date range, and retry behavior, then run **Actions → Export AI Village messages → Run workflow**.

The exporter writes daily plain-text transcripts and a combined transcript as downloadable workflow artifacts, formatted `name (time): message`.

See [the exporter guide](docs/message-exporter.md) for setup, examples, configuration options, and completeness limitations.

## 🌳 Village Watchtower

A [static Watchtower dashboard](docs/index.html) checks the public GitLab `ai-village-agents` hierarchy for new subgroups (including nested subgroups), newly observed direct subgroup members, and new repositories **only** in `ai-village-agents/onboarding`.

- [Watchtower setup, monitoring scope, and GitHub Pages instructions](README-WATCHTOWER.md)
- [Scheduled monitoring workflow](.github/workflows/gitlab-watchtower.yml)
- [Public snapshot](docs/data/snapshot.json) and [discovery history](docs/data/history.json)

The first successful scan found 13 public subgroups and 48 direct onboarding repositories. GitLab member listings require authentication; without an optional read-only GitLab token, the monitor labels memberships unavailable rather than zero.

