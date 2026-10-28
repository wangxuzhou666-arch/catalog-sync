# catalog-sync

[Chinese (zh-CN)](README.zh-CN.md) · **v0.1.0rc1 release candidate** · MIT

## Bring data from the services you use into the platform you build.

For technical teams connecting SaaS or enterprise data services to their own applications: **collect authorized data → clean and map it → load it**, in one repeatable workflow.

For example, bring an authorized supplier catalog into an internal purchasing system, with consistent units and repeatable updates.

### The problem

Getting access to external data is only the start. Your team still needs to align fields, clean formats and handle failed or repeated imports. catalog-sync brings those steps into one workflow with explicit, reusable rules and a record of what happened.

### What it helps you do

- **Create consistent records.** Map fields, parse units and apply missing-value rules without inventing missing data.
- **Repeat imports with clear update rules.** Resume collection and preserve application-owned fields according to your configuration.
- **Avoid collecting the same data just to change a mapping.** Keep raw responses, reprocess them offline and identify failed items.

### The workflow

```mermaid
flowchart TB
    A["Authorized external data"] --> B["Collect and cache"]
    B --> C["Clean and map fields"]
    C --> D["Your database or business application"]
```

Configure the **source, transformation rules and destination**. Real sources need a compatible connector; business applications need a receiving endpoint or adapter.

### One command. A complete result.

Install and start Docker, download this repository, then run from its root:

```sh
sh tools/demo.sh
```

The demo collects synthetic data, cleans it, imports it and opens a report showing **before-and-after values, stored records and repeat-import checks**. No real account or host Python installation is needed.

It includes **24 materials and 9 suppliers**; materials are written to both a local database and a reference receiver. The first run downloads dependencies. Use macOS, Linux, or Windows with Docker-integrated WSL.

### Scope and validation

The first version covers material and supplier records, with the framework, synthetic source and connector scaffolding. Adapt real services yourself. Generic web scraping and automated CAPTCHA bypass are outside this release.

The candidate code passed Python 3.11–3.13 checks, the Docker workflow and **18 acceptance checks**. See [tested scope and limitations](docs/acceptance.md); this does not establish compatibility with arbitrary external services.

[Full user guide](docs/README.md) · [Build a connector](docs/connector-guide.md) · [Data rules](docs/data-contract.md) · [Receiver contract](docs/platform-contract.md) · [Deployment](docs/deploy.md)

Only process data you are authorized to access and export, and follow the source service's terms. [Responsible use](docs/compliance.md) · [Security](SECURITY.md) · [License](LICENSE)
