<div align="center">

# Ouroboros Snake Strategy

Glitch's flagship multi-bot ensemble, combining Oracle coordination with the six-snake execution stack.

![Product](https://img.shields.io/badge/product-Flagship%20Ensemble-0f766e?style=for-the-badge)
![Platform](https://img.shields.io/badge/platform-MT5%20today%20%7C%20cTrader%20next-111827?style=for-the-badge)
![Architecture](https://img.shields.io/badge/architecture-Oracle%20%2B%20Six%20Snakes-1d4ed8?style=for-the-badge)
![Family](https://img.shields.io/badge/family-Glitch%20Ecosystem-7c3aed?style=for-the-badge)

[![Hub](https://img.shields.io/badge/Hub-Glitch%20Trading%20Core-111827?style=for-the-badge&logo=github)](https://github.com/glitch-executor/glitch-trading-core)
[![Flagship](https://img.shields.io/badge/Flagship-Ouroboros%20Snake%20Strategy-0f766e?style=for-the-badge&logo=github)](https://github.com/glitch-executor/glitch-ouroboros-snake-strategy)
[![Satellite](https://img.shields.io/badge/Satellite-Indian%20King%20Cobra-1d4ed8?style=for-the-badge&logo=github)](https://github.com/glitch-executor/glitch-indian-king-cobra)
[![Satellite](https://img.shields.io/badge/Satellite-Terciopelo-7c3aed?style=for-the-badge&logo=github)](https://github.com/glitch-executor/glitch-terciopelo)

</div>

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

- [Glitch Trading Core](https://github.com/glitch-executor/glitch-trading-core) as the umbrella architecture repo
- [Indian King Cobra](https://github.com/glitch-executor/glitch-indian-king-cobra) as the standalone unified momentum scalper
- [Terciopelo](https://github.com/glitch-executor/glitch-terciopelo) as the standalone equities relative-value strategy

## Why It Exists

Ouroboros is the public-facing strategy identity for the main Glitch ensemble.

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

## Documentation

- [Architecture](./docs/architecture.md)
- [Operating Model](./docs/operating-model.md)
- [Platforms](./docs/platforms.md)
- [MT5 Track](./mt5/README.md)
- [cTrader Track](./ctrader/README.md)

## License

Released under [Apache 2.0](./LICENSE) with attribution preserved through [NOTICE](./NOTICE).
