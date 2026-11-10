# Village Watchtower 🌳

A read-only public GitLab monitor for `ai-village-agents`, published as a GitHub Pages website from the `docs/` directory in [`Minuteandone/Village-data`](https://github.com/Minuteandone/Village-data).

## What is monitored

- **Any new public descendant subgroup**, even several levels below the root group. Uses GitLab's paginated `/groups/:id/descendant_groups` endpoint.
- **Direct member additions** in each public descendant subgroup, including nested groups. Uses each group's paginated `/groups/:id/members` endpoint, not inherited membership lists (which would artificially duplicate parent members).
- **New public repositories directly in `ai-village-agents/onboarding` only.** Uses `/groups/:id/projects?include_subgroups=false&with_shared=false`, plus an explicit namespace check. Projects elsewhere, including nested onboarding groups, are excluded.

A GitHub Actions workflow runs automatically every **two hours**, on manual dispatch, and when its source files change. A first run builds the baseline without posting fake discoveries. Later runs compare complete snapshots and add detections to `docs/data/history.json` (up to the 2,500 latest events), while `docs/data/snapshot.json` keeps the current view.

If any API request fails or a paginated scan cannot finish, the job fails and leaves the previously published data untouched. It never requires a GitLab token and cannot view private members or groups.

## Enable the website (one-time owner action)

1. Visit **[repository Settings → Pages](https://github.com/Minuteandone/Village-data/settings/pages)**.
2. Under **Build and deployment**, choose **Deploy from a branch**.
3. Select **main** and folder **/docs**, then **Save**.
4. The website will be accessible at **https://minuteandone.github.io/Village-data/** after GitHub Pages finishes deploying.

The repository's GitLab scan workflow is independent of whether Pages is enabled. See the **[Actions](https://github.com/Minuteandone/Village-data/actions/workflows/gitlab-watchtower.yml)** tab to run it manually or inspect whether the public GitLab API permitted each check.

## Files

- `.github/workflows/gitlab-watchtower.yml` — scheduled polling, offline tests, commit updated data.
- `scripts/gitlab_watch.py` — API pagination, nested hierarchy, membership and project scoping, diff engine.
- `tests/test_gitlab_watch.py` — offline tests covering nested groups, direct members, onboarding-only projects, unchanged scans, API failures, pagination.
- `docs/index.html`, `docs/styles.css`, `docs/app.js` — static accessible dashboard, search/filter controls, expandable directory and onboarding repository list.
- `docs/data/snapshot.json`, `docs/data/history.json` — published data for the site.

## Limitations

This monitors publicly visible GitLab data only. Some group member lists may be inaccessible to anonymous API calls; if so, the workflow fails closed rather than inventing empty membership lists. This is a **discovery monitor**, not a full GitLab audit log: events are time-stamped when the scheduled check discovers them, not necessarily when the GitLab action happened. Additions occurring and disappearing entirely between scans may be missed. Cron scheduling is best-effort; GitHub may delay a scheduled run. History is capped to control repository size.

## Run tests locally

```bash
python -m unittest discover -s tests -p 'test_gitlab_watch.py' -v
```
