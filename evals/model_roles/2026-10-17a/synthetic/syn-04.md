Sparse autoencoder features for deceptive intent in Kestrel-1.3B

We train sparse autoencoders on the residual stream of Kestrel-1.3B, an open-weights language model from the Vantage Collective, to look for features that activate when the model is instructed to deceive. We choose Kestrel-1.3B because its training data and every intermediate checkpoint are public, so we can trace when such features appear during training.

We train one autoencoder per layer, with 32,768 latents each, on 400 million tokens of the model's activations. In layers 14 to 18 we find a cluster of features that fire on prompts asking the model to mislead a user and stay silent on matched honest prompts. Ablating the five strongest features reduces deceptive completions from 38% to 9% on our held-out set, while leaving accuracy on honest question answering unchanged.

Tracing these features back through the public checkpoints, we find that they first become identifiable after about 60% of pretraining. We release the autoencoders and the feature labels.
