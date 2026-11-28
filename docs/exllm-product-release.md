# EXLLM product build and release

Pushing a commit to `exllm-dev` starts **EXLLM product build** in GitHub Actions. The job builds the embedded console with Bun and the Linux amd64 server with Go. It also packages the static site from the same commit. Download the `exllm-product-<commit>` artifact from the successful run.

| File | Contents |
| --- | --- |
| `new-api-linux-amd64.tar.gz` | Executable `new-api`, embedded console, and license files |
| `new-api-image-linux-amd64.tar.gz` | Docker image archive tagged `exllm-new-api:<commit>` |
| `exllm-site.tar.gz` | The tracked `exllm-site/` directory, including the home page, model market, and branding assets |
| `BUILD-METADATA` | Git commit, application version, and target platform |
| `SHA256SUMS` | Archive checksums |

Verify the downloaded files with `sha256sum -c SHA256SUMS` on Linux. Check that the commit in `BUILD-METADATA` matches the intended Actions run. The build runs the binary's version command and checks its embedded commit.

## Production integration

This workflow builds artifacts. Production deployment requires an approved release workflow and an authorized receiver that can access the target system. Configure the receiver and the deployment approval in a private operations context before adding a production job. No server credentials are used by this build.

For the PostgreSQL rollout, load the image from the approved run into the private deployment environment. See the [Compose and migration runbook](../deploy/postgres/README.md). The release integration should stage the corresponding site archive from the same run. Install the executable through the existing New API service release procedure. Stage the site assets alongside the current public site version, keeping its documentation and other assets, then atomically switch the static release pointer. Keep the previous binary and static release available throughout verification. The payment bridge, runtime configuration, and database are managed separately.

After activation, check the homepage, model market, sign-in page, and `/api/status`. Check the deployed application version and the static content against the approved build. If verification fails, restore the previous executable and static release pointer, restart the application only if its binary was replaced, and repeat the checks. A release that requires a database migration needs its own compatibility and recovery review before activation.
