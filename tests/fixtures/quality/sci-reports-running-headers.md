rimary conclusions and indicate that training methodology and model scale jointly shape robust multimodal visual reasoning performance. 

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

The evaluation landscape for AI models has evolved beyond traditional means to include benchmarks that assess model capabilities and performance. For example, while MMBench<sup>19</sup> and SEED-Bench<sup>20</sup> measure model performance using multiple-choice questions, MM-Vet<sup>21</sup> measures open-ended visual chat using GPT-4-based scoring. MME<sup>22</sup> offers binary yes/no testing of cognitive and perceptual ability. TextVQA<sup>23</sup> focuses on reading text contained within an image. POPE<sup>24</sup> uses object-based probing to determine whether a model has produced hallucinations. Visual Question Answering (VQA) has usually been assessed using the VQAv2 benchmark<sup>25</sup> . Recent reviews<sup>1,2</sup> of current trends in these evaluation benchmarks have documented their existence but did not conduct any quantitative synthesis or analysis of cross-evaluation metrics. 

### **Meta-analysis in machine learning** 

In Evidence-Based Medicine (EBM)<sup>26,27</sup> , meta-analysis and multilevel modelling are recognised as foundational methods for studies of causal relationships, but their applications in the field of Machine Learning (ML) have been relatively limited. However, several recent developments in computer vision and multimodal reasoning, such as spatio-temporal vision-language models<sup>28</sup> and knowledge-driven visual question answering<sup>29</sup> , have resulted in do so a surge of new models and task-specific capabilities. To this point, most existing surveys of MLLM<sup>1,2,30,31</sup> only in a qualitative sense, by providing a categorisation of the existing models and task-specific capabilities, without a quantitative or statistically rigorous synthesis. We seek to fill this gap by adapting existing meta-analysis procedures<sup>7,32</sup> for use in the area of MLLM performance literature. 

However, several adaptations must be made to traditional approaches to evidence synthesis in machine learning, beyond simply the level at which studies are aggregated. In the MLLM context, each model produces multiple benchmark results; the data therefore have a hierarchical structure and exhibit substantial structural heterogeneity. This supports the use of a multilevel primary model, and it provides the basis for reporting aggregate randomeffects estimates as sensitivity analyses and for reporting robustness diagnostics that explicitly test the extent to which the conclusions are driven by individual models, benchmarks, or subgroup imbalances. 

Scientific Reports 

Article in Press 

https://doi.org/10.1038/s41598-026-72982-1 

## **Methodology** 

### **Study selection and data extraction** 

We have compiled benchmark data for 21 MLLMs with parameters ranging from 6B to 1.8T (i.e., 6 billion to 1.8 trillion parameters) published in 2023–2024. Models were included based on the following criteria: at least two publicly available benchmark scores were reported across 7 benchmarks; a vision-language architecture that has an LLM backbone and a visual encoder was used; all scoring was under standard evaluation procedures. Each model’s benchmark score, parameter count, vision encoder type, LLM backbone and training methodology were collected. Scores were compiled from the original model publications, from the evaluation tables of the benchmark papers themselves, and from public benchmark leaderboards; the source of each individual value is recorded in the data provenance table. The benchmarks used and their standard evaluation protocols are described in their respective sources<sup>19,21,22,33</sup> . 

### **Protocol and provenance controls** 

To enhance the reproducibility and accountability of analyses, we established a predetermined analytical protocol before beginning the statistical analysis of the models used in the current study. As a result, there were only 7 named benchmark sets available (i.e., all models with 2 or more benchmark values were included; if a model was missing one or more benchmark values, those benchmarks were recorded as missing rather than imputed), and the model-benchmark records were retained with a key that was used to identify the source of each benchmark value. Therefore, all analysed benchmark values can be retraced to their source record through the resulting provenance table, which contains all the information of models such as metadata, benchmark identifier(s), the raw scores, the publication years, and the corresponding source key; ultimately, 102 records can be traced back to their sources. This retrospective (post hoc) analysis was not registered in advance. Table 1 summarises the included models and their architectural characteristics. 

**Table 1** Overview of 21 MLLMs included in the meta-analysis. 

_Nb_ , number of benchmarks reported; PT+Align,  Pretraining + Alignment; IT , instruction tuning; RLHF,  reinforcement learning from human feedback. 

|Model|Vision encoder|Params|_N_b|LLM backbone|Training|
|---|---|---|---|---|---|
|Fuyu-8B|Linear Proj.|8.0B|3|Persimmon-8B|PT+Align|
|MiniGPT-4|EVA-ViT-G|8.0B|3|Vicuna-7B|PT+Align|
|BLIP-2|EVA-ViT-G|12.1B|5|FlanT5-XXL|PT+Align|
|InstructBLIP-7B|EVA-ViT-G|7.0B|6|Vicuna-7B|IT|