# Evaluation report

Batch `7df926c41b5d…` — coverage 95.42%

## Winter flag
precision 0.7656 / recall 0.9800 (tp=49 fp=15 fn=1)

## Categories
end-to-end accuracy 0.9835 / conditional-on-decode 0.9835

Expected-decode abstentions and quarantines count as end-to-end misses; coverage is reported separately.

| category | precision | recall | f1 | support |
|---|---|---|---|---|
| Apparel | 1.0 | 1.0 | 1.0 | 36 |
| Braking | 0.9783 | 0.9783 | 0.9783 | 46 |
| Electrical | 1.0 | 0.9286 | 0.963 | 28 |
| Filtration | 0.9714 | 1.0 | 0.9855 | 34 |
| Visibility | 0.9744 | 1.0 | 0.987 | 38 |

## Attributes

| attribute | accuracy | n |
|---|---|---|
| material | 1.0 | 182 |
| pack_count | 1.0 | 182 |
| position | 1.0 | 182 |
| size | 0.989 | 182 |

## Confusion pairs

- Braking/Rotors → Filtration/Air Filters ×1
- Electrical/Ignition Coils → Visibility/Wiper Blades ×1
- Electrical/Spark Plugs → Braking/Calipers ×1
