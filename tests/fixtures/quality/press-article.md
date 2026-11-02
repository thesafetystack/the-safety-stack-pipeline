# **Pooled estimates of reward-model calibration drift across fourteen fine-tuning runs**

## Abstract

Submitted 3 March 2026; accepted 30 August 2026; open access, © 2026 the authors

Example Research Letters 

### ARTICLE | Article in Press 

# **Pooled estimates of reward-model calibration drift across fourteen fine-tuning runs** 

## **Mirela Quandt · Osei Varnhagen · Lio Sabatelli · Petra Ondrusko** 

Submitted 3 March 2026; accepted 30 August 2026; open access, © 2026 the authors 

#### **Abstract** 

Reward models are trained once and then used for many rounds of policy optimisation, yet little is known about how far their confidence moves away from the preferences they were fitted to while the policy changes underneath them. We collect calibration measurements from fourteen fine-tuning runs of four model families, each scored at six checkpoints, and pool them with a hierarchical model in which checkpoints are nested within runs. The pooled expected calibration error rises from 0.041 at the first checkpoint to 0.118 at the last, and most of the variance sits between runs rather than within them (intra-run correlation 0.64). Runs that refreshed a fifth of their preference data halfway through drifted about half as much as runs that never refreshed it. The size of the reward model mattered less than expected once the refresh schedule was taken into account. A leave-one-family-out analysis gives the same ordering of effects, and a funnel plot shows no sign that small runs report systematically smaller drift. We argue that calibration should be reported per checkpoint as routinely as reward accuracy, and we release the pooled table and the analysis scripts so that later runs can be added to it. 

**Keywords** Reward models · Calibration · Preference learning · Hierarchical models · Distribution shift 

## **Introduction** 

Fine-tuning a language model against a learned reward is now a common step in building assistants, and the reward model that drives it is usually judged by a single number: how often it agrees with held-out human comparisons. That number is measured on the distribution of responses the reward model was trained on. Once optimisation starts, the policy produces responses that drift away from that distribution, and the agreement rate on the original test split says little about how trustworthy the reward is on the new outputs. 

Calibration is a natural lens for this problem. A calibrated reward model that assigns a probability of 0.8 to one response being preferred should be right about four times in five. If calibration degrades as the policy moves, the optimiser is following a signal whose confidence is no longer earned, and the familiar symptoms of over-optimisation follow. 

Article in Press: this manuscript has been peer reviewed and accepted. It is posted before copy-editing and typesetting, and the version of record may differ in layout and wording. 

Example Research Letters 

https://doi.org/10.5555/erl.2026.0417 

Article in Press 

Single studies of this effect exist, but they differ in model families, preference datasets, checkpoint spacing and the calibration measure itself, which makes it hard to say whether drift is large or small in general. We therefore treat the question as one for quantitative synthesis: we gather measurements that share a common protocol, put them on one scale, and model the dependence that comes from scoring the same run several times. 

Our contributions are as follows. First, a pooled table of 84 run-checkpoint observations with a documented extraction protocol. Second, a hierarchical estimate of drift over the course of optimisation, with its between-run and within-run variance. Third, a moderator analysis of the refresh schedule, the reward model's size and the policy's size. Fourth, a set of sensitivity checks, including leave-one-family-out estimates and a test for small-study effects. 

## **Methods** 

We included a run when it reported reward-model calibration on a fixed comparison set at no fewer than four checkpoints, together with the size of the reward model and whether its training data were refreshed during optimisation. Two of us extracted each run independently; disagreements, which concerned the checkpoint spacing of three runs, were settled by reading the run's configuration file. 

Expected calibration error was computed with fifteen equal-mass bins on each run's own comparison set. Because comparison sets differ in difficulty, we standardised each run's errors against its first checkpoint, so that the outcome is the change in calibration since optimisation began rather than its absolute level. 

The model has a fixed slope for the checkpoint index, random intercepts and slopes for runs, and fixed effects for the three moderators. We fitted it by restricted maximum likelihood and report normal-approximation intervals; a fit with weakly informative priors gave estimates within two hundredths of these on every coefficient. 

For sensitivity we refitted the model four times, each time leaving out one model family, and once more with the checkpoint index replaced by the number of optimisation steps, since runs did not space their checkpoints evenly. Small-study effects were examined with a funnel plot of run-level slopes and a regression of the slope on its standard error. 

## **Results** 

The average run drifted steadily: each checkpoint added 0.015 to the expected calibration error (95% interval 0.011 to 0.019), with no evidence that the rate slowed towards the end. Between-run variance was large, and the intra-run correlation of 0.64 means that knowing one checkpoint of a run tells a good deal about its others. 

Refreshing the preference data was the strongest moderator. Runs that replaced a fifth of their comparisons halfway through drifted at 0.008 per checkpoint against 0.017 for runs that did not, and the difference held in every leave-one-family-out fit. The reward model's size had a small and uncertain effect once refresh was in the model; the policy's size had none that we could detect. 

The four families differ in their starting calibration but much less in how fast they lose it. The family with the best initial error also had the steepest slope, so that by the last checkpoint its advantage had mostly gone; the other three were ordered at the end as they were at the start. Measuring progress by optimisation steps instead of checkpoints changed the slope's units but not its interval relative to its size. 

The funnel plot of run-level slopes against their standard errors is close to symmetric, and a regression test for small-study effects does not reject symmetry. Two runs with unusually steep drift both used a reward model distilled from a larger one; removing them lowers the pooled slope slightly but changes no conclusion. 

## **Discussion** 

Drift in calibration is not an occasional failure of particular runs but a regular feature of reward-driven fine-tuning, and its size depends more on how the reward model is maintained than on how large it is. The practical reading is modest: report calibration at every checkpoint, and budget for refreshing preference data during long runs. 

Our synthesis has limits. The runs were selected because they reported calibration at all, which may favour careful teams. The comparison sets are not shared across runs, so absolute levels are not comparable and we model only change. And fourteen runs are few for a moderator analysis; the refresh effect is clear, but the size effects should be read as weak evidence. 

Two extensions would make the table more useful. A shared comparison set, scored by every reward model at every checkpoint, would allow absolute levels to be pooled as well as change. And recording which responses the policy produced at each checkpoint would let later work ask whether drift concentrates on the kinds of response the optimiser favours, which is the mechanism most accounts of over-optimisation assume but few measurements test directly. 

## **References** 

1. Abelard, T. & Neumark, S. Calibration of pairwise preference models under policy shift. _Example Proceedings on Learning Systems_ **12**, 101–118 (2024). 

2. Brunsvik, H. et al. Over-optimisation of learned rewards: a controlled study. _Example Transactions on Machine Inference_ **7**, 44–61 (2025). 

3. Castellane, R., Moorby, J. & Ito, K. Equal-mass binning for calibration error estimates. _Example Statistics Letters_ **31**, 9–20 (2023). 

4. Delacroix-Pym, A. Refreshing preference data during long optimisation runs. Preprint at https://example.org/papers/refresh-2025 (2025). 

5. Eskelund, M. & Farrow, D. Hierarchical models for repeated measurements of model quality. _Example Review of Applied Statistics_ **18**, 233–250 (2022). 

6. Ferreira-Holm, L. et al. Distilled reward models and their failure modes. _Example Proceedings on Learning Systems_ **13**, 77–90 (2025). 

7. Gorecki, P. Small-study effects in meta-analyses of machine learning results. _Example Methods Quarterly_ **5**, 12–27 (2024). https://doi.org/10.5555/emq.2024.0012 
