# Deploy Shmamale on Railway

The repository includes everything Railway needs: a pinned Python runtime,
production dependencies, a single-worker Socket.IO start command, and a health
check.

## Deploy from GitHub

1. Sign in at [Railway](https://railway.com/) and choose **New Project**.
2. Choose **Deploy from GitHub repo** and select `rpowergso/shmamale`.
3. Wait for the build and deployment to become healthy.
4. Open the service's **Settings > Networking** section and choose
   **Generate Domain**.
5. Open the generated `https://...up.railway.app` address, create a play room,
   and send its four-character room code or full room URL to friends.

No database or custom environment variable is required for the first
deployment. Railway supplies `PORT`, and the application generates an ephemeral
Flask secret if `SECRET_KEY` is not set.

## Important scaling note

Keep this service at **one replica**. Active rooms currently live in the web
process's memory. A deployment, restart, or scale-to-zero wake-up clears active
games, and multiple replicas would not share rooms. A future Redis-backed room
store can remove this limitation.

## Automatic deploys

When the service is connected to GitHub, pushes to `main` can deploy
automatically. Railway reads `railway.json` on each deployment, checks `/health`,
and only then routes traffic to the new version.
