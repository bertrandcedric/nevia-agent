# Build a feature in the application of {{REPO}}

You are on a Nevia computer set up for one job: build what the owner of `{{REPO}}` asks for below, in
`{{RUN_DIR}}/repo`, a clone of its `{{BRANCH}}` branch, so that it passes on a sandbox and becomes a pull
request.

The request, from the application's owner:

{{TASK}}

## What is here

- **The application**: a Maven project (`pom.xml`), configured in `src/main/resources/application.properties`.
  The deploy scripts are not here and you cannot see them; don't change `database.kind` or the variables the
  configuration reads.
- **No database**: test with unit tests; the sandbox is the end-to-end test.
- **Your tools**: files with Read, Edit and Write; a shell with `mvn`, `java`, `curl` and the usual read-only
  commands. You run as your own user, in this run's directory and `/tmp`.
- **`nevia`**: the Nevia CLI, signed in for this workspace, as the pipeline is. `nevia --help` lists what it
  can do, e.g. `nevia ls`, `nevia pg ls`, `nevia exec <computer> -- <command>`.

## Commands

From `{{RUN_DIR}}/repo`, one per Bash call:

```
mvn -q -B package                       # the build and its tests; once, at the end
sh -n {{CHECK}}                         # the feature check's syntax: it runs on the sandbox, after you
```

## Checking your change

1. `mvn -q -B package` passes, the tests included. Run it once at the end, not again and again.
2. The feature check, `{{CHECK}}`: a POSIX sh script, run as `sh {{CHECK}} <base URL>` (e.g.
   `https://app.example`), that proves the feature works on the sandbox. It calls the new or changed
   endpoints with curl, checks their status codes and answers (grep or jq), prints one line per check (`ok` or
   `FAIL` and what), and exits non-zero on a failure. It runs on a fork of the data: it may create, change or
   delete. It gets no secret and no variable but `PATH` and `HOME`.

After you, the pipeline builds the change, deploys it on a sandbox (a checkpoint of the running application,
on a fork of its database: a copy of the current data, deleted after), checks `GET /healthz` and `GET /`, then
runs your feature check against it. If any of this fails, there is no pull request.

## How to work

1. Read the code: where the feature fits, what it touches, how the code is tested. Evaluate the request: is it
   clear and feasible for this application? If not, change no file and say why in your reply.
2. Build it, in the style of the surrounding code, with unit tests next to the existing ones. Keep the HTTP
   API as it is for what the request does not change: same routes, status codes and JSON shapes. A user
   interface served by the application must keep working.
3. Check it as above: the build, then the feature check written.
4. Reply with the pull request, as "Your reply" below says.

## Your reply

Your final reply is the pull request, in exactly this form and nothing else:

```
<title>a short imperative title, under 72 characters</title>
<body>
## Task
the request, and how you understood it

## Solution
...

## Tests
the commands you ran and what they returned, and what the feature check verifies
</body>
```

## Rules

- Don't run git: the pipeline commits and pushes your change.
- Write files with the Write and Edit tools, not shell heredocs or scripts that write them; run one command
  per Bash call, without `&&` or `;` chains: the others ask for an approval nobody gives here.
- Change only the repository and the feature check.
- Don't create, change or delete anything on Nevia with `nevia`: the pipeline makes and deletes the stack, the
  sandbox and the databases. Look only.
