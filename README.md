<div align="center">

# Ouroboros Snake Strategy

Glitch's flagship multi-bot ensemble, combining Oracle coordination with the six-snake execution stack.

![Product](https://img.shields.io/badge/product-Flagship%20Ensemble-0f766e?style=for-the-badge)
![Family](https://img.shields.io/badge/family-glitch%20trading-111827?style=for-the-badge)
![Platform](https://img.shields.io/badge/platform-MT5%20today%20%7C%20cTrader%20next-111827?style=for-the-badge)
![Scope](https://img.shields.io/badge/scope-oracle%20%7C%20ensemble%20%7C%20risk-1d4ed8?style=for-the-badge)

[Glitch Trading Core](https://github.com/glitch-exec-labs/glitch-trading-core) · [Indian King Cobra](https://github.com/glitch-exec-labs/glitch-indian-king-cobra) · [Terciopelo](https://github.com/glitch-exec-labs/glitch-terciopelo)

</div>

> Ouroboros Snake Strategy is the flagship coordinated Glitch ensemble: six specialized execution bots, one Oracle layer, and a portfolio-aware risk model designed to keep the stack coherent.

## Glitch Trading Family

```mermaid
flowchart LR
    A[Glitch Trading Core] --> B[Ouroboros Snake Strategy]
    A --> C[Indian King Cobra]
    A --> D[Terciopelo]
    B --> E[Flagship Multi-Bot Ensemble]
```

## Repo Role

Ouroboros Snake Strategy is the flagship coordinated Glitch ensemble.

It combines:

- `oracle.py` as the coordination and conflict-resolution layer
- `viper.py`
- `cobra.py`
- `taipan.py`
- `mamba.py`
- `anaconda.py`
- `hydra.py`

## Part Of The Glitch Ecosystem

It sits alongside:

- [Glitch Trading Core](https://github.com/glitch-exec-labs/glitch-trading-core) as the umbrella architecture repo
- [Indian King Cobra](https://github.com/glitch-exec-labs/glitch-indian-king-cobra) as the standalone unified momentum scalper
- [Terciopelo](https://github.com/glitch-exec-labs/glitch-terciopelo) as the standalone equities relative-value strategy

## Why It Exists

Ouroboros is the public-facing flagship identity for the main Glitch ensemble.

The goal is to keep:

- six complementary execution styles
- one coordinated portfolio brain
- reusable risk controls
- broker-portable architecture over time

## System At A Glance

```mermaid
flowchart LR
    A["Market Data"] --> B["Six Snake Bots"]
    B --> B1["Viper"]
    B --> B2["Cobra"]
    B --> B3["Taipan"]
    B --> B4["Mamba"]
    B --> B5["Anaconda"]
    B --> B6["Hydra"]
    B1 --> C["Oracle"]
    B2 --> C
    B3 --> C
    B4 --> C
    B5 --> C
    B6 --> C
    C --> D["Risk + Portfolio Guards"]
    D --> E["Execution Adapter"]
    E --> F["MT5 Today / cTrader Next"]
```

## Repo Layout

```text
glitch-ouroboros-snake-strategy/
|-- mt5/
|   |-- bots/
|   |-- shared/
|   `-- configs/
|-- ctrader/
|   `-- README.md
`-- docs/
```

## Bot Roles

| Bot | Style | TF | Role |
| --- | --- | --- | --- |
| `viper.py` | momentum + pullback | M5 | fast directional execution |
| `cobra.py` | structure + price action | H1 | higher-conviction structure logic |
| `taipan.py` | session breakout | M30 | expansion capture |
| `mamba.py` | mean reversion | M15 | range balance |
| `anaconda.py` | breakout confirmation | H4 | slower structural continuation |
| `hydra.py` | regime routing | M1 | adaptive tactical layer |
| `oracle.py` | coordination | multi-bot | portfolio governor |

## Public Repo Safety

- only sanitized example configs are included
- no live credentials, state, models, logs, or training data are committed
- secrets should live outside Git

## Quick Start

1. Read [Architecture](./docs/architecture.md) for the ensemble shape.
2. Review [Operating Model](./docs/operating-model.md) for Oracle coordination and bot responsibilities.
3. Compare the current and next platform tracks in [mt5/README.md](./mt5/README.md) and [ctrader/README.md](./ctrader/README.md).
4. Treat configs in Git as sanitized examples only.

## Contributing

The best public contributions here are documentation clarity, sanitized examples, and platform-portability improvements that keep the ensemble design understandable. Start with [CONTRIBUTING.md](./CONTRIBUTING.md).

## Documentation

- [Architecture](./docs/architecture.md)
- [Operating Model](./docs/operating-model.md)
- [Platforms](./docs/platforms.md)
- [Social Preview Brief](./docs/social-preview.md)
- [MT5 Track](./mt5/README.md)
- [cTrader Track](./ctrader/README.md)

## Maintainer And Contact

Glitch Executor is developed and maintained by Tejas Karan Agrawal, operating under the business name Nuraveda.

- Support and responsible disclosure: `support@glitchexecutor.com`
- Registered address: `77 Huntley St, Toronto, ON M4Y 2P3, Canada`

## License

Released under [Apache 2.0](./LICENSE) with attribution preserved through [NOTICE](./NOTICE).
