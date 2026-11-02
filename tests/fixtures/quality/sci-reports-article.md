# **A multilevel meta-analysis of visual reasoning in multimodal large language models**

## Abstract

Received: 19 April 2026 / Accepted: 21 September 2026 © The Author(s) 2026

Scientific Reports 

### ARTICLE | Article in Press 

# **A multilevel meta-analysis of visual reasoning in multimodal large language models** 

## **Piyush Sharma · Varsha Sahni · Rijwan Khan · Himanshu Gupta** 

Received: 19 April 2026 / Accepted: 21 September 2026 © The Author(s) 2026 

#### **Abstract** 

Multimodal Large Language Models (MLLMs) have become a leading approach to visual reasoning, yet the field still lacks a quantitative synthesis that rigorously identifies the drivers of their performance. In this paper, we evaluate 21 models across 7 standardised benchmarks, yielding 102 model–benchmark observations, using a two-level mixed-effects design in which benchmark scores are nested within models. The multilevel model reveals substantial between-model variance (intra-class correlation coefficient, ICC = 0.801) and a significant positive association between model scale and standardised performance (β = 0.73, _p_ = 0.016). Training strategy also matters: relative to instruction tuning, the pretraining + alignment approach is associated with substantially lower performance (β = −1.88, _p_ < 0.001), while early descriptive trends suggest that the Reinforcement Learning from Human Feedback (RLHF) group may be associated with positive effects, although only two models fall within this group, which prevents any definitive conclusion. Correlations between benchmarks are generally strong, ranging from ρ = 0.63 to 0.95 among the reliably estimated pairs. As a sensitivity analysis, we also report a DerSimonian–Laird aggregate meta-analysis, which yields a near-zero pooled effect (d = − 0.046) and extreme heterogeneity (I² = 96.3%). Robustness checks — Hedges’ g validation, leave-one-out sensitivity, bootstrap confidence intervals, SE-floor sensitivity, and Pareto analysis — support the primary conclusions and indicate that training methodology and model scale jointly shape robust multimodal visual reasoning performance. 

**Keywords** Multimodal large language models · Meta-analysis · Visual reasoning · Benchmark evaluation · Vision-language models · Random-effects model 

## **Introduction** 

Advancements in Large Language Models (LLMs) and visual understanding capabilities have led to Multimodal LLMs (MLLMs) that combine visual encoders with large language backbones, enabling complex visual reasoning<sup>1,2</sup> . Since the emergence of multimodal GPT-4 and Gemini, MLLM architecture has rapidly proliferated from proprietary systems with trillions of parameters to smaller, efficient open-source systems with fewer than 10 billion parameters<sup>3–5</sup> . 

This Article in Press is shared early to give you faster access to new research. It is citable and carries a permanent DOI. The final edited version will replace it automatically. 





Scientific Reports 

https://doi.org/10.1038/s41598-026-72982-1 

Article in Press 

https://doi.org/10.1038/s41598-026-72982-1 

While MLLM development is advancing at an unprecedented pace, we lack a systematic quantitative synthesis of the factors that impact performance in visual reasoning tasks. Typically, an individual paper describes its results by comparing its model to a few baselines on selected benchmarks, making it difficult to establish a systematic view of all of the models in the field<sup>2</sup> . Many crucial questions remain unanswered, including. 

- What impact does scale have on visual reasoning<sup>6</sup> ? 

- Is the choice of vision encoder more important than the training strategy? 

- Do different benchmarks measure fundamentally different abilities, or are they measuring similar abilities? 

This methodological gap is compounded by existing heterogeneity in benchmark sets, testing strategies and scoring formats. As such, it is difficult to make direct point-estimate comparisons among studies, even though they often appear on the same leaderboard. Different levels of benchmark coverage and model family evaluation under differing protocol assumptions will very often cause point-estimate comparison to greatly overestimate or underestimate true performance differences between the evaluated models. 

Therefore, a synthesis framework that explicitly models dependence and heterogeneity is essential. In the context of MLLMs, the presence of multiple benchmark outcomes for the same model will create clusters of observations that violate the independence assumptions employed in simple aggregate comparisons. If this structure is considered explicitly, it will create greater identification of between-model versus within-model variability, allowing for more defensible moderator analyses and more transparent methods of checking sensitivity. 

This study relies on meta-analysis, which is a well-established statistical framework used for synthesising the results of independent studies<sup>7,8</sup> . In our situation, benchmark scores are nested within models, and therefore, a multilevel mixed-effects design is the most appropriate form of primary analysis. The use of classical random effects syntheses as an alternate (secondary) benchmark will allow for a method of comparative analysis between multilevel estimates and a standard aggregate approach. Further analysis of the data through meta-regression will also assist in identifying which architectural or training characteristics are the strongest predictors of performance. 

The current paper presents a quantitative synthesis of MLLM performance via visual reasoning through a multilevel methodology. Specifically, we: 

1. Compile benchmark results for 21 leading MLLMs across 7 standardised benchmarks, creating a structured dataset with 102 model-benchmark observations; 

2. Fit a two-level mixed-effects model using random intercepts at the model level to account for benchmark observations nested within models as well as to estimate variance components, intraclass correlation coefficients (ICCs), and moderator effects; 

3. Retain the DerSimonian–Laird random-effects aggregate analysis as a secondary sensitivity check to compare our multilevel results against conventional model-level synthesis; 

4. Conduct moderator analyses through mixed-effects regression and subgroup comparisons, yielding a qualitative understanding of how model scale, vision encoder architecture, and training strategy affect performance; 

5. Assess publication bias via Egger’s regression test<sup>9</sup> and trim and fill methodology<sup>10</sup> . 

6. Characterise inter-benchmark relationships through Spearman rank correlation analysis to reveal clusters of benchmarks that measure overlapping versus non-overlapping capabilities. 

## **Related work** 

### **Multimodal large language models (MLLMs)** 

The MLLM paradigm is built on the ideas of using the Vision Transformer<sup>11</sup> and Contrastive Vision-Language Pre-training<sup>12</sup> . Early works, such as Flamingo<sup>13</sup> , showed that a frozen LLM could have visual perception added to 

Scientific Reports 

https://doi.org/10.1038/s41598-026-72982-1 

Article in Press 

it through cross-attention. BLIP-2<sup>5</sup> created the Q-Former module that connects frozen image encoders to frozen LLMs, thus forming a lightweight alignment method. 

The visual instruction tuning method, which was developed by LLaVA<sup>14</sup> , showed that by fine-tuning the entire model on a visual instruction dataset, there was a significant increase in multimodal reasoning performance. Research has explored many design decisions, including the vision encoder used (CLIP<sup>12</sup> , EVA<sup>15</sup> , and SigLIP<sup>16</sup> , the capacity of the model’s LLM backbone (7B–34B parameters, where B denotes billion), and different training methodologies that included using Reinforcement Learning from Human Feedback (RLHF)<sup>17</sup> , and chain-ofthought reasoning<sup>18</sup> . 

Even though these studies provide important architecture and training trends, they tend to focus on giving model-specific claims rather than providing for cross-model inference. Many studies have reported improvements related to specific benchmark subsets/evaluations, making it difficult to determine whether the improvements were all due to a single factor or due to various contributing factors, as outlined in previous studies, including scaling, data curation, alignments, and benchmarks themselves. These challenges present a further rationale for viewing the models in a synthesis-oriented manner using a common, normalised framework and preserving the estimates of uncertainty and heterogeneity associated with each of the models analysed. 

### **MLLM benchmarks** 

The evaluation landscape for AI models has evolved beyond traditional means to include benchmarks that assess model capabilities and performance. For example, while MMBench<sup>19</sup> and SE

## **References** 

1. Zhang, D., Yu, Y., Dong, J., Li, C., Su, D., Chu, C. & Yu, D. MM-LLMs: Recent advances in multimodal large language models. In _Findings of the Association for Computational Linguistics: ACL 2024_ , (2024). 

2. Davide Caffagni, F. et al. The revolution of multimodal large language models: A survey. In _Findings of the Association for Computational Linguistics: ACL 2024_ (2024). 

3. Haotian Liu, C., Li, Y., Li & Lee, Y. J. Improved baselines with visual instruction tuning. In _Proceedings of the IEEE/ CVF Conference on Computer Vision and Pattern Recognition (CVPR)_ (2024). 

4. Dai, W. et al. InstructBLIP: Towards general-purpose vision-language models with instruction tuning. _Adv. Neural. Inf. Process. Syst._ , **36** , (2023). 

5. Li, J., Li, D., Savarese, S. & Hoi, S. BLIP-2: Bootstrapping language-image pre-training with frozen image encoders and large language models. In _Proceedings of the Inter-national Conference on Machine Learning (ICML)_ (2023). 

6. Kaplan, J., McCandlish, S., Henighan, T., Brown, T.B., Chess, B., Child, R., Gray, S., Radford, A., Wu, J. & Amodei, D. Scaling laws for neural language models. arXiv preprint arXiv:2001.08361 (2020). 

7. Borenstein, M., Hedges, L. V., Julian, P. T., Higgins & Hannah, R. Rothstein. Introduction to Meta-Analysis. 2nd edn., (Wiley, 2021). 

8. Larry, V. _Hedges and Ingram Olkin. Statistical Methods for Meta-analysis_ (Academic, 1985). 

9. Egger, M., Smith, G. D., Schneider, M. & Minder, C. Bias in meta-analysis detected by a simple, graphical test. _BMJ_ **315** (7109), 629–634 (1997). 

10. Duval, S. & Tweedie R. Trim and fill: A simple funnel-plot–based method of testing and adjusting for publication bias in meta-analysis. _Biometrics_ **56** (2), 455–463 (2000). 

11. Alexey Dosovitskiy, L. et al. An image is worth 16x16 Words: Transformers for image recognition at scale. In _International Conference on Learning Representations (ICLR)_ , (2021). 

12. Alec Radford, J.W. et al. Learning transferable visual models from natural language supervision. In _Proceedings of the International Conference on Ma-chine Learning (ICML)_ (2021). 

13. Alayrac, J. B. et al. Flamingo: A visual language model for few-shot learning. _Adv. Neural. Inf. Process. Syst._ **35** , 23716– 23736 (2022). 

14. Liu, H., Li, C., Wu, Q. & Lee, Y.J. Visual instruction tuning adv. _Neural Inform. Process. Syst._ **36** , (2023). 

15. Yuxin, F. et al. Xin-long Wang, and Yue Cao. EVA: Exploring the limi