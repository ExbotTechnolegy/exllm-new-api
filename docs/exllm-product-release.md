# EXLLM product build and release

Pushing a commit to `exllm-dev` starts **EXLLM product build** in GitHub Actions. The job builds the embedded console with Bun and the Linux amd64 server with Go. It also packages the static site from the same commit. Download the `exllm-product-<commit>` artifact from the successful run.

| File | Contents |
| --- | --- |
| `new-api-linux-amd64.tar.gz` | Executable `new-api`, embedded console, and license files |
| `new-api-image-linux-amd64.tar.gz` | Docker image containing the same executable, labeled with the full Git commit and application version |
| `exllm-site.tar.gz` | The tracked `exllm-site/` directory, including the home page, model market, and branding assets |
| `BUILD-METADATA` | Git commit, application version, and target platform |
| `SHA256SUMS` | Archive checksums |

Verify the downloaded files with `sha256sum -c SHA256SUMS` on Linux. Check that the commit in `BUILD-METADATA` matches the intended Actions run. The build runs the binary's version command and checks its embedded commit.

## Release and rollback

The maintainer runs **EXLLM production release** from Actions on `exllm-dev`. For `deploy`, enter the full SHA of a successful product build on that branch. For `rollback`, leave the commit blank to restore the previous image and static release together. Both operations use the same GitHub concurrency group and the host site's release lock.

The receiver verifies the build metadata and checksums, loads the image, and stages a static directory beside the current release. Existing docs and other assets carry forward when the package omits them. It replaces only the `new-api` Compose service's image setting and atomically swaps the site pointer. It checks the origin API version, exact homepage, model market and docs content, and console HTML at `/sign-in` and `/keys`. If an activation fails, it restores and verifies both previous pointers and the Actions run fails. If a release is interrupted, dispatch `rollback` to finish restoring the last confirmed release. Keep the recorded current and previous site directories and Docker images available.

Use this workflow for versions that can start and roll back against the existing database schema. PostgreSQL 16, Redis, payment bridge, Nginx and their volumes are managed independently. An image rollback does not undo application schema changes or customer writes. After a successful release, check the public domain from outside the host as well.

## One-time maintenance setup

1. In the repository's Actions environments, use `exllm-production` with a deployment branch rule for `exllm-dev` and no required reviewers. Set environment variable `EXAI_RELEASE_MAINTAINER` to the maintainer's GitHub login. Protect write access to the product branch and its workflows. [GitHub environment settings](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)
2. As the host administrator, install [receive-release.py](../deploy/postgres/receive-release.py) and [release-ssh-entry.sh](../deploy/postgres/release-ssh-entry.sh) in the root-owned `/usr/local/libexec/exai-release/` directory, with mode `0755`. The host needs Python 3.8+, current Docker Compose v2 and curl. Create a private root-owned state directory. Put that path and the **existing** production Compose and static settings in root-only `/etc/exai-release.json`. Use the following keys, replacing all sample values.

   ```json
   {
     "state_dir": "/var/lib/exai-release",
     "compose_project": "exai",
     "compose_file": "/srv/exai/compose.yml",
     "compose_env": "/srv/exai/vars.env",
     "site_root": "/srv/exai/site",
     "origin_host": "public.example.com"
   }
   ```

   The site root must already have `releases/` and a `current` symlink to a directory directly in `releases/`. Use the same site lock for all changes. The current private Compose env must have one `EXAI_IMAGE=` assignment. The checked-in [Compose template](../deploy/postgres/compose.yml) describes the topology. Point the receiver at the production configuration already running against the existing volumes.

3. Set up a dedicated SSH account named `exai-release` with no password or Docker-group access. Give it a shell for the forced command and a single dedicated public key with these authorized-key options.

   ```text
   restrict,command="/usr/local/libexec/exai-release/release-ssh-entry.sh" ssh-ed25519 <deployment public key>
   ```

   Install a root-owned, `visudo`-validated sudoers entry for the receiver's two deployment operations.

   ```sudoers
   exai-release ALL=(root) NOPASSWD: /usr/local/libexec/exai-release/receive-release.py rollback, /usr/local/libexec/exai-release/receive-release.py deploy *
   ```

4. Run `sudo /usr/local/libexec/exai-release/receive-release.py bootstrap <running-image-commit-sha>` once. This checks and records the currently healthy image and static directory as the first rollback target. The SHA must match the image revision label or the full commit in its local tag. Keep other release paths from switching `current` or the application image after this handoff.
5. Add the three secrets to the **environment**, using values supplied by the maintainer. Verify the host fingerprint on the host or through the provider console before supplying it. The runner connects to the standard SSH port.

   | Secret | Value |
   | --- | --- |
   | `EXAI_DEPLOY_TARGET` | Dedicated account and SSH hostname such as `exai-release@release.example.com` |
   | `EXAI_DEPLOY_SSH_KEY` | Private key for that account only |
   | `EXAI_DEPLOY_KNOWN_HOSTS` | Verified OpenSSH known-hosts line for the exact hostname |

   Check that a remote command other than `deploy <sha>` or `rollback` is rejected before the first production dispatch.
