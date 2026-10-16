# Model manifest

The reverse-design workflow uses DeepFrag2 2.0.0 with the `gte_4_best` model and fragment fingerprint database.

| File | Size in source deployment | SHA-256 |
| --- | ---: | --- |
| `gte_4_best.ckpt` | 19,868,360 bytes | `e832bafd66a2209c8ca861df93a00d854437990236f94a7acdcd7c017c63d601` |
| `gte_4_all_sets_frags.smi` | 132,352 bytes | `7a111c5250ba5bac3001740257972f61ccac952d78e0644667672a387df93f2a` |
| `gte_4_all_sets_frags.smi_rdk10_fps.bin` | 71,768,529 bytes | `6e7aa5e083e89cf3e4441431431a05155091a71f05e8c486862f0551550222ab` |

The files are stored under `Reverse Design/pretrained_models/`. Verify them before use:

```bash
sha256sum "Reverse Design/pretrained_models/"*
```

DeepFrag2 project page: <https://durrantlab.pitt.edu/deepfrag2/>

DeepFrag2 source repository: <https://github.com/durrantlab/deepfrag2>

DeepFrag2 is licensed under the MIT License. See `THIRD_PARTY_NOTICES.md` and the upstream project for attribution and current installation guidance.
