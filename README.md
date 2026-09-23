# CMN-C1-036 — PersonalizationContextBuilderAgent

> **Category**: Cat 1 (a single technical capability, generic and reusable)
> **Industry**: Common / Cross-Industry

## Overview

This agent ingests a stream of user behavioral events (page views, purchases, searches, and
similar interactions) and builds a structured user context profile: ranked interest topics,
price sensitivity, preferred channels, and recency/frequency/engagement scores. Interest topics,
price sensitivity, and preferred channels are inferred by a real LLM call (Azure OpenAI); a
missing/unconfigured key or an LLM-call failure degrades to a deterministic heuristic rather than
failing the request. Recency/frequency/engagement scores are always deterministic time-decay math.
It deliberately
does not perform recommendation, adaptive UI rendering, or content selection — those decisions
belong to downstream systems that consume the profile this agent produces. It also enforces a
pseudonymisation boundary: only de-identified user identifiers are accepted, and any output
profile that would resolve to a raw identifier (email, phone number) is blocked rather than
emitted.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent raises
`PlatformRequired` during graph compile / start-up preflight rather than starting in a partially
working state. This is intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/` for the design document and test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
