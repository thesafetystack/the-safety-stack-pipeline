# Robustness to one kind of noise does not buy robustness to another | Example Lab

Robustness to one kind of noise does not buy robustness to another
Abstract
We ask whether training a vision model to withstand one family of input corruptions makes it more robust to families it never saw. Across eight corruption families and three training recipes, robustness transfers well between corruptions that change local texture, poorly between texture and geometry, and in one case negatively: training against blur makes models measurably more sensitive to small rotations. Mixing families during training recovers most of the lost robustness at a modest cost in clean accuracy. We release the corruption suite and the trained models so that the transfer matrix can be extended to new families.
Read paper at https://example.org/papers/noise-transfer
