ments were taken on the same comparison set at every checkpoint, which is what makes the within-run differences interpretable at all. 

Article in Press: this manuscript has been peer reviewed and accepted. It is posted before copy-editing and typesetting, and the version of record may differ in layout and wording. 

2 

Example Research Letters 

https://doi.org/10.5555/erl.2026.0417 

Article in Press 

## **Data extraction** 

Each run was described by a short record: the model family, the sizes of the policy and of the reward model, the number and spacing of checkpoints, whether and when the preference data were refreshed, and the comparison set used for calibration. Two of us filled in the record independently from the run's report and its configuration file, and a third compared the two copies field by field. Of the 168 fields extracted in this way, eleven differed between the copies; nine were transcription slips and two concerned the spacing of checkpoints in runs that had been resumed after an interruption. 

Calibration was not always reported in the form we needed. Five runs gave reliability diagrams rather than a summary number; for these we read the bin frequencies off the published diagram with a digitising tool and recomputed the error with our own binning. We checked the procedure on two runs that reported both a diagram and a number, and the recomputed values agreed with the reported ones to within 0.003. 

Where a run reported several comparison sets, we used the one closest to the preference data the reward model was trained on, so that the first checkpoint measures calibration in the setting the model was fitted for. This choice was fixed before any outcome was extracted. 

## **Outcome and standardisation** 

The outcome is the change in expected calibration error since the first checkpoint. Absolute errors are not comparable across runs, because comparison sets differ in how many near-ties they contain, and near-ties are where a pairwise model is hardest to calibrate. Subtracting the first checkpoint removes the level of each run and keeps its trajectory. 

3 

Example Research Letters 

https://doi.org/10.5555/erl.2026.0417 

Article in Press 

We considered dividing by the first checkpoint instead of subtracting it, which would express drift as a proportion. Two runs began with an error below 0.01, and their proportional drift would have dominated the pooled estimate for reasons that have nothing to do with optimisation, so we kept the difference and report the proportional version only as a sensitivity check. 

Checkpoints were indexed from zero at the start of optimisation. Runs that spaced their checkpoints unevenly were also coded by the number of optimisation steps at each checkpoint, rescaled so that the longest run ends at one. 

## **Statistical model** 

Let the outcome of run i at checkpoint t be the change in calibration error. We model it as a run-specific line through the origin plus noise, with the run's slope drawn from a normal distribution whose mean depends on the moderators. The origin is fixed because the outcome is zero at the first checkpoint by construction; a free intercept, which we tried, was estimated at 0.001 with an interval that included zero. 

The moderators enter as fixed effects on the slope: an indicator for a mid-run refresh of the preference data, the logarithm of the reward model's parameter count, and the logarithm of the policy's. Residual variance was allowed to differ between families, since one family reported calibration on a much smaller comparison set and its measurements are noisier. 

4 

Example Research Letters 

https://doi.org/10.5555/erl.2026.0417 

Article in Press 

The model was fitted by restricted maximum likelihood. Intervals are normal-approximation intervals on the fixed effects; for the variance components we report profile intervals, which behave better when the number of runs is small. As a check on the frequentist fit, we also fitted the model with weakly informative priors on all variance components and compared posterior means with the point estimates. 

## **Sensitivity analyses** 

Four analyses probe how much the conclusions depend on choices we made. The first leaves out one model family at a time and refits the model. The second replaces the checkpoint index by the rescaled step count. The third uses proportional instead of absolute drift. The fourth removes the two runs whose reward models were distilled from larger ones, because their trajectories look different from the rest on visual inspection. 

A fifth analysis concerns the selection of runs rather than the model. Runs that report calibration at all may be the careful ones, and careful runs may drift less. We cannot test this directly, but we can ask whether runs with fewer checkpoints, which are typically shorter and less documented, show systematically different slopes; a funnel plot of slopes against their standard errors and a regression of one on the other address this. 

5 

Example Research Letters 

https://doi.org/10.5555/erl.2026.0417 

Article in Press 

## **Software and data** 

All analyses were run in a single script that reads the extracted records, builds the outcome, fits the models and writes every table and figure in the paper. The records, the script and the digitised reliability diagrams are released together at https://example.org/data/erl-calibration-drift so that a reader can reproduce each number and add further runs to the table. 

## **Results** 

Fourteen runs met the inclusion criteria, contributing 84 run-checkpoint observations. Eight runs came from two families with public configuration files; the remaining six came from reports whose authors answered our questions about checkpoint spacing by correspondence. No run was excluded after extraction. 

Calibration error at the first checkpoint ranged from 0.008 to 0.071 across runs, a spread that confirms that absolute levels could not have been pooled. By the last checkpoint every run had a higher error than at its first, and in twelve of the fourteen the increase was monotone apart from at most one checkpoint. 

6 

Example Research Letters 

https://doi.org/10.5555/erl.2026.0417 

Article in Press 

The pooled slope is 0.015 per checkpoint. Between-run variance in the slope is about twice the average within-run residual variance, and the profile interval for the slope's standard deviation excludes zero comfortably. The model with family-specific residual variances fits better than the one with a common variance by a clear margin in the likelihood ratio, as expected from the one family with a small comparison set. 

Refreshing the preference data halfway through roughly halves the slope. The effect is visible in the raw trajectories as well: the refreshed runs bend downward at the refresh and then resume a shallower climb, while the others keep a steady rise. The size of the reward model enters with a negative coefficient whose interval includes zero, and the size of the policy with a coefficient close to zero. 

Leaving out one family at a time moves the pooled slope between 0.013 and 0.017 and the refresh effect between 0.007 and 0.011; no single family drives either result. Using the step count instead of the checkpoint index, or proportional instead of absolute drift, leaves the ordering of the moderators unchanged. 

The two runs with distilled reward models have the steepest slopes of all fourteen. Without them the pooled slope falls to 0.013 and the refresh effect is unchanged. The funnel plot is roughly symmetric, and the regression of slopes on their standard errors gives a coefficient whose interval includes zero, so we see no evidence that shorter, less documented runs report systematically different drift. 

Table 2 lists the slope of every run together with its moderators, and Figure 3 overlays the fourteen trajectories on the pooled line with its interval. The family with the best initial calibration loses its advantage by the fourth checkpoint, which is the clearest single illustration that a good starting error says little about how a reward model will hold up under optimisation. 
