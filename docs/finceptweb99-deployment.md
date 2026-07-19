# FinceptWeb99 deployment

The production deployment is intentionally bound to one stack:

- repository: `/opt/finceptweb99`
- branch: `main`
- remote: `https://github.com/x812033727/FinceptWeb99.git`
- Compose project: `finceptweb99`
- health endpoint: `http://127.0.0.1:8081/api/health`
- trigger, status, lock, and log directory: `/opt/finceptweb99/var`

The runner refuses missing configuration, a different repository, remote or
branch, and tracked or staged changes. Untracked operational files are kept.
Every deploy creates and independently verifies a full PostgreSQL backup before
fetching, then fast-forwards, builds all application images, explicitly runs
both migration ledgers, and verifies health and container state. Backend and
scheduler are stopped only after builds complete.

Install the reviewed runner and the dedicated units after merging:

```bash
install -m 0755 scripts/finceptweb-deploy.sh \
  /usr/local/bin/finceptweb99-deploy.sh
install -m 0644 docker/systemd/finceptweb99-deploy.service \
  /etc/systemd/system/finceptweb99-deploy.service
install -m 0644 docker/systemd/finceptweb99-deploy.path \
  /etc/systemd/system/finceptweb99-deploy.path
systemctl daemon-reload
systemctl enable --now finceptweb99-deploy.path
```

Do not replace or modify the separate `finceptweb-deploy.*` units. They belong
to `/opt/finceptweb`, which is a different deployment.

Trigger a deployment through the admin UI or explicitly with:

```bash
touch /opt/finceptweb99/var/deploy-trigger
```

Follow progress with `systemctl status finceptweb99-deploy.service`,
`journalctl -u finceptweb99-deploy.service`, and
`/opt/finceptweb99/var/deploy-status.json`. A successful document has
`phase: "completed"`, the before/after SHA, `branch: "main"`, and actor/trigger
metadata when initiated by the admin UI.
