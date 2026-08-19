"""PR81B — VLM model-sensitivity matrix over the PR81A experiment.

PR81A promoted ``narrow_rerank_only`` on one answerer/reranker identity
(gemma-4-26b via the local gateway). PR81B re-runs the same corpus,
lanes, and declared decision rule across several vision-capable models,
adds hybrid ablation lanes that decompose the gain (answer modality,
rerank vs union recall), and applies the confirmation rule declared in
``decision`` — committed before any matrix numbers were looked at.
"""
