# Publish this project to GitHub (Windows CMD)

## 1. Create the GitHub repository

1. Open https://github.com/new and sign in.
2. Repository name: `telegram-booking-agent` (or another name you prefer).
3. Choose **Public** if the employer needs to open the code from the submitted link.
4. Do **not** initialise the repository with a README, license or `.gitignore` because these files are already in this project.
5. Click **Create repository** and keep the page open to copy the repository URL.

## 2. Extract this cleaned archive

Extract `booking_agent_github_ready.zip` to a normal folder, for example:

`C:\Users\YOUR_USER\Downloads\booking_agent_github_ready`

Open CMD in that folder (File Explorer address bar: type `cmd` and press Enter).

## 3. Initialise and inspect before committing

Run these commands one at a time:

```cmd
git --version
git init
git branch -M main
git add .
git status --short
git check-ignore .env
```

`git check-ignore .env` should print `.env`. If it does not, **stop and do not commit**.

Check that `git status --short` does not list `.env`, any `*.session`, `decisions.log`, `logs/`, `.venv/` or API keys.

## 4. Commit and push

Replace the URL below with the URL from the new GitHub repository page:

```cmd
git commit -m "Initial submission: Telegram booking agent"
git remote add origin https://github.com/YOUR_USERNAME/telegram-booking-agent.git
git push -u origin main
```

GitHub may open a browser sign-in window.

## 5. Final security check

Open the repository page and confirm that `.env`, session files and runtime logs are absent. Never upload your `.env` or `booking_agent.session`.

If a key was accidentally included in any public repository or shared archive, revoke it in the provider dashboard and create a new one.
