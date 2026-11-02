e LLMs across the Pythia family (Biderman et al., 2023b), which provide finegrained access to checkpoints and training data and for which an exhaustive searches of duplicated and memorized sequences exists. We focus on Pythia-1B as our object of study, though we perform all experiments for models of size 160m, 410m, 1B, 1 _._ 4B, 2 _._ 8B, and 6 _._ 9B, unless stated otherwise. 

3 

Preprint 



<!-- Start of picture text -->
30<br>10 3.5 1<br>864 2.51.532 25201510 0.80.60.4 Class control (rare)control (duplicated)recited<br>2 1 5 0.2 recollected<br>0.5 0 0<br>0<br>0 2 8 32 128 512 2k 4k 6k 8k 10k 10k 30k 50k 70k 90k 110k 130k 10k 30k 50k 70k 90k 110k 130k 10k 30k 50k 70k 90k 110k 130k<br>Step Step Step Step k<br>(a) Loss until step 10k (b) Loss after step 10k (c) Num. 32-extr. (d) Frac. of extractable tokens<br>y<br>e extractabilit<br>g<br>Cross entropy loss Cross entropy loss<br>Num. 32-extr. sequences Avera<br><!-- End of picture text -->

Figure 1: **Pythia-1B training dynamics.** We plot the loss and extractability of our sampled sequences over time and find that a lot of memorization happens early in training. Other models in App. Fig. 9. 

For all numeric results and figures we present, we link to a version in the Appendix containing the same data for the other models. We study memorized sequences over training and decompose their cross entropy (CE) loss, which we sample from an exhaustive dataset of memorized sequences by Biderman et al. (2023a) for each model. We use their definition of memorization as 32-extractability, i.e., whether, given the first 32 tokens of the sequence, the model greedily generates the next 32 tokens verbatim. We filter out sequences with repeating subsequences (”reconstruction” in Prashanth et al. (2025)’s taxonomy) and samples of code, which we found to often contain boilerplate sequences, resulting in memorized examples resembling natural language. We follow Prashanth et al. (2025)’s taxonomy and further discriminate between **recollected** (memorized sequences that appear up to five times in the training set) and **recited** sequences (memorized sequences that appear more than five times in the training set). To contextualize our results, we include two control groups, **control** and **control-duplicated** , whose samples also occur up to and more than five times, respectively, but are not 32-extractable in the final model. For each of the four classes, we sample 32 sequences and study their decomposition. Details on datasets and implementations in App. B. 

To our surprise, we could not verify 32-extractability for around 10 _−_ 20% sequences sampled from Biderman et al. (2023a)’s dataset across model sizes. To investigate this discrepancy, we plot the average fraction of the correct continuation token being the maximum logit prediction across all sequences in Fig. 1d. The problem only shows for a small fraction of tokens, which indicates the problem is likely due to small numeric differences of our hardwa