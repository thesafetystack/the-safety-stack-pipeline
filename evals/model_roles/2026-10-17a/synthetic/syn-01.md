Zephyrine-9B: An Open Model Trained for Calibrated Refusal

Abstract. We introduce Zephyrine-9B, a 9-billion-parameter decoder-only language model released by Halcyon Labs under an open-weights licence. Zephyrine-9B is pretrained on 3.1 trillion tokens and then post-trained with direct preference optimization on 140,000 preference pairs that reward refusing clearly harmful requests while answering borderline ones. On our over-refusal suite, Zephyrine-9B refuses 4.2% of benign borderline prompts, against 17.8% for Marrowind-3, the strongest open model of similar size, while matching Marrowind-3's refusal rate on clearly harmful prompts.

Method. Starting from the pretrained checkpoint, we run two rounds of preference optimization. Round one uses pairs labelled by annotators; round two uses pairs where Zephyrine-9B's own earlier responses were rewritten by annotators. We release both the base and the post-trained weights.

Evaluation. We compare Zephyrine-9B with Marrowind-3 on three refusal benchmarks and on four capability benchmarks. Marrowind-3 is run with its published system prompt and default sampling settings. Zephyrine-9B is within one point of Marrowind-3 on every capability benchmark, so the reduction in over-refusal does not come at a measurable capability cost.

Limitations. Our harmful-prompt set is in English only, and we did not test multi-turn jailbreaks.
