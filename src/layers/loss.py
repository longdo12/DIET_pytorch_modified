import torch
import torch.nn as nn
from torch.nn.functional import pairwise_distance, normalize, nll_loss,log_softmax, binary_cross_entropy_with_logits

class ContrastiveLoss(nn.Module):
  def __init__(self, m=2.0):
    super(ContrastiveLoss, self).__init__()  # pre 3.3 syntax
    self.m = m  # margin or radius

  def forward(self, y1, y2, d=0):
    # d = 0 means y1 and y2 are supposed to be same
    # d = 1 means y1 and y2 are supposed to be different
    euc_dist = pairwise_distance(y1, y2)

    if d == 0:
      return torch.mean(torch.pow(euc_dist, 2))  # distance squared
    else:  # d == 1
      delta = self.m - euc_dist  # sort of reverse distance
      delta = torch.clamp(delta, min=0.0, max=None)
      return torch.mean(torch.pow(delta, 2))  # mean over all rows


class SingleLabelDotProductLoss(nn.Module):
    """Single-label dot-product loss layer.

    This loss layer assumes that only one output (label) is correct for any given input.
    """
    def __init__(
        self,
        mu_pos: float = 0.8,
        mu_neg: float = -0.2,
        use_max_sim_neg: bool = True,
        neg_lambda: float = 0.5,
        same_sampling: bool = False,
    ) -> None:
        """Declares instance variables with default values.

        Args:
            mu_pos: Indicates how similar the algorithm should
                try to make embedding vectors for correct labels;
                should be 0.0 < ... < 1.0 for `cosine` similarity type.
            mu_neg: Maximum negative similarity for incorrect labels,
                should be -1.0 < ... < 1.0 for `cosine` similarity type.
            use_max_sim_neg: If `True` the algorithm only minimizes
                maximum similarity over incorrect intent labels,
                used only if `loss_type` is set to `margin`.
            neg_lambda: The scale of how important it is to minimize
                the maximum similarity between embeddings of different labels,
                used only if `loss_type` is set to `margin`.
        """
        super(SingleLabelDotProductLoss, self).__init__()

        self.mu_pos = mu_pos
        self.mu_neg = mu_neg
        self.use_max_sim_neg = use_max_sim_neg
        self.neg_lambda = neg_lambda

    def sim(self, a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
        """Calculates similarity between `a` and `b`.

        Operates on the last dimension. When `a` and `b` are vectors, then `sim`
        computes either the dot-product, or the cosine of the angle between `a` and `b`.

        We compute `a . b / (|a| |b|)`.

        Args:
            a: Any float tensor
            b: Any tensor of the same shape and type as `a`
            mask: Mask (should contain 1s for inputs and 0s for padding). Note, that
                `len(mask.shape) == len(a.shape) - 1` should hold.

        Returns:
            Similarities between vectors in `a` and `b`.
        """
        
        a = normalize(a, p= 2, dim=-1)
        b = normalize(b, p= 2, dim=-1)
        sim = torch.sum(a * b, dim=-1)
        
        return sim
    def _loss_margin(
        self,
        sim_pos: torch.Tensor,
        sim_neg: torch.Tensor,
    ) -> torch.Tensor:
        """Define max margin loss."""
        loss = torch.maximum(torch.Tensor([0]).to(sim_pos.device), self.mu_pos - torch.squeeze(sim_pos, axis=-1))

        if self.use_max_sim_neg:
            # minimize only maximum similarity over incorrect actions
            max_sim_neg = torch.max(sim_neg)
            loss += torch.maximum(torch.Tensor([0]).to(max_sim_neg.device), self.mu_neg + max_sim_neg)
        else:
            # minimize all similarities with incorrect actions
            max_margin = torch.maximum(torch.Tensor([0]).to(sim_neg.device), self.mu_neg + sim_neg)
            loss += torch.max(max_margin)
        return torch.mean(loss)
    def _scale_loss(self, log_likelihood: torch.Tensor) -> torch.Tensor:
        """Creates scaling loss coefficient depending on the prediction probability.

        Arguments:
            log_likelihood: a tensor, log-likelihood of prediction

        Returns:
            Scaling tensor.
        """
        p = torch.exp(log_likelihood)
        # only scale loss if some examples are already learned
        return torch.where(
            torch.max(p) > 0.5,
            torch.pow((1 - p) / 0.5, 4).detach(),
            torch.ones_like(p),
        )
    def _loss_cross_entropy(
        self,
        sim_pos: torch.Tensor,
        sim_neg: torch.Tensor,
    )   -> torch.Tensor:
        """Defines cross entropy loss."""

        # Compute softmax loss

        # Similarity terms between input and label should be optimized relative
        # to each other and hence use them as logits for softmax term
        logits = torch.cat((torch.reshape(sim_pos,(sim_pos.size()[0], 1)), torch.reshape(sim_neg,(sim_neg.size()[0], 1))), dim=-1)
        # create label_ids for softmax
        softmax_label_ids = torch.zeros_like(logits[..., 0]).long().to(sim_pos.device)
        soft_max_loss = nll_loss(log_softmax(logits, dim=-1), softmax_label_ids)

        # Compute sigmoid loss 

        # Constrain similarity values in a range by applying sigmoid
        # on them individually so that they saturate at extreme values.
        sigmoid_labels = torch.cat([torch.ones_like(logits[..., :1]),torch.zeros_like(logits[..., 1:])], dim=-1).to(sim_pos.device)

        sigmoid_loss = binary_cross_entropy_with_logits(logits,sigmoid_labels)
        sigmoid_loss = torch.mean(sigmoid_loss, dim=-1)
 
        loss = soft_max_loss + sigmoid_loss

        loss *= self._scale_loss(-loss)
        if len(loss.shape) == 2:
            loss = torch.mean(loss, dim=-1)

        return loss

    def forward(  
        self,
        inputs_embed: torch.Tensor,
        labels_embed_positive: torch.Tensor,
        labels_embed_negative: torch.Tensor,
    ) -> torch.Tensor:
        """Calculate loss.

        Args:
            inputs_embed: Embedding tensor for the batch inputs;
                shape `(batch_size, ..., num_features)`
            labels_embed_positive: Embedding tensor for the batch positive labels;
                shape `(batch_size, ..., num_features)`
            labels_embed_negative: Embedding tensor for the batch positive labels;
                shape `(batch_size, ..., num_features)`
            
        Returns:
            loss: Total loss.
        """

        sim_pos = self.sim(inputs_embed, labels_embed_positive.float())
        sim_neg = self.sim(inputs_embed, labels_embed_negative.float())

        loss = self._loss_margin(sim_pos,sim_neg)
        # loss = self._loss_cross_entropy(sim_pos,sim_neg)

        return loss