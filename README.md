# GitHub Pages ZIP publication PoC

This folder is a self-contained test package. Copy its **contents** to the root of a new, dedicated GitHub repository before enabling Actions. The nested `.github/workflows/publish-pages.yml` is intentionally not discoverable from the Personal Portfolio repository root.

The workflow is manual only. It validates `fixtures/demo-work.zip`, safely extracts it to `works/zip-demo/`, checks local HTML/CSS asset references, uploads one Pages artifact, then deploys it. It never executes the uploaded JavaScript in Actions.

## Test content and limits

The fixture is synthetic and safe to publish. A GitHub Free Pages site requires a public repository, so **every branch and file committed to that test repository is public**, including the ZIP fixture. Do not put private drafts or real unpublished work in this PoC repository.

Current PoC limits (conservative test policy, not GitHub platform quotas): ZIP <= 1 MiB, <= 100 archive entries and <= 20 regular files, <= 10 MiB expanded total, <= 2 MiB per file, and <= 100:1 compression ratio. Only stored/deflated files with a small static extension allowlist are accepted. The archive must contain a root `index.html`. Symlinks, encrypted entries, path traversal, absolute paths, backslashes, duplicate/case-colliding paths, and nested archives are rejected.

## Run local validation

From this directory:

```bash
python3 -m unittest discover -s tests -v
python3 scripts/safe_extract.py fixtures/demo-work.zip --dest /tmp/pages-poc-site/works/zip-demo
python3 scripts/check_relative_assets.py /tmp/pages-poc-site/works/zip-demo
```

The test suite uses only Python's standard library and temporary directories. It covers the valid fixture and rejected malicious/over-limit archives. The `safe_extract.py` command refuses an existing destination; use a fresh temporary directory for each run.

## Manually publish after repository setup

1. Create a **new public repository** for this PoC (GitHub Free Pages requirement) and copy this folder's contents to its root. Do not connect it to the production Vercel project.
2. In **Settings → Pages → Build and deployment**, select **GitHub Actions**.
3. In **Settings → Actions → General**, keep workflow permissions read-only; the workflow declares its own minimal job permissions.
4. Commit the PoC files to the default branch. In **Actions**, select **Publish GitHub Pages ZIP PoC**, choose the default branch, and click **Run workflow**.
5. Once the run succeeds, open the `github-pages` environment URL. The demo is at `works/zip-demo/` under the Pages base URL.

The repository owner must have repository admin access to configure Pages. The GitHub Pages deployment job requires `pages: write` and `id-token: write`; the build job only gets `contents: read` and `pages: read`. No PAT, deploy key, or external service is used.

## Failure recovery and rollback

- If validation fails, the build job fails and the deploy job is skipped; the current Pages deployment remains unchanged. Fix or replace the ZIP and rerun the workflow from the default branch.
- If the deploy job fails, inspect its Actions log and rerun the failed job/workflow. The deployment consumes the complete artifact produced by the successful build job; there is no per-file live update.
- To roll back a published version, revert the commit that changed `fixtures/demo-work.zip` (or restore the previous ZIP from Git history), commit that revert to the default branch, then manually run the workflow again. The Pages deployment will publish the restored complete site. Alternatively, rerun the earlier successful workflow run if GitHub still allows it.
- Git history and workflow logs are the audit trail. Pages hosts only the latest deployment at the site URL; this PoC does not create permanent preview URLs for each version.

## Limits and security caveats

- Project Pages uses a repository path, for example `https://OWNER.github.io/REPOSITORY/`. The demo uses relative resource paths. Root-absolute URLs such as `/assets/app.css` would point at the host root and fail for a project site.
- GitHub Pages sites are public. GitHub Pages project sites for one owner share the `OWNER.github.io` origin across repository paths; a published work can interact with same-origin content/storage of other Pages projects owned there. The main portfolio on its Vercel hostname is a different origin. Do not host admin APIs or credentials on the Pages origin.
- GitHub Pages official limits include a 1 GiB published site, a soft 100 GB/month bandwidth limit, a 10-minute deployment timeout and a soft 10 builds/hour limit (the latter does not apply to a custom GitHub Actions workflow). The Pages artifact must be under 10 GB; the PoC archive limits are much smaller.
- GitHub Actions workflow dispatch is not an upload endpoint. This PoC has no website admin authentication or browser upload. A future backend must authenticate the sole administrator server-side and hold any GitHub credential server-side. Vercel Function request/response bodies are limited to 4.5 MB, so a typical ZIP must not be POSTed through a Function; GitHub workflow-dispatch inputs are parameters, not an arbitrary file channel. If a ZIP is committed to a public Pages repository, its bytes become public immediately, before Pages deploys.
- Safe extraction validates archive structure; it cannot certify that arbitrary HTML/JavaScript is benign. The workflow does not run the uploaded code. Keep this test repo and the Pages site separate from the portfolio admin origin.
