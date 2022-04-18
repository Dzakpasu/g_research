# coding=utf-8
# Copyright 2022 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Helper to interactively calibrate the size of randomly-generated examples."""

from typing import Any, Callable, Iterable, Optional, Tuple

import jax
import jax.numpy as jnp
import numpy as np
import optax
import scipy

from gfsa import automaton_builder
from gfsa.datasets import graph_bundle


def calibrate_padding(
    example_builder,
    desired_sizes,
    success_probability = 0.9,
    samples = 1000,
    min_object_size = 16,
    max_object_size = 512,
    optimization_max_steps = 10000,
    log_likelihood_epsilon = 1e-2,
    round_to_powers_of_two = False,
    progress = None,
):
  """Determine an example size that fits in a given padding config.

  (Note: This function is designed to be used interactively as part of setting
  up a new dataset.)

  The sizes of examples as generated by example generators doesn't always
  match the actual size of the example once it has been encoded. To enable
  running with static batch sizes, we want to determine the sizes of the actual
  inputs to the model. The inputs, however, depend on the schema encoding of the
  specific nodes that are chosen, and (for instance) not all AST nodes are the
  same size in the graph representation.

  This method takes a desired padding configuration, which specifies a bound
  on the sizes of the inputs that we want to get close to. It then generates
  a bunch of data, fits a simple model to the sizes of the generated data, and
  then uses that model to determine what initial object size we should generate
  to make sure that we fit within the desired padding with probability at least
  `success_probability`.

  Since some parts of the input are likely smaller than others, we also return
  a smaller padding config that still suffices to hold the generated examples.

  (More specifically, we model the size of each example as a draw from a normal
  distribution, where the mean and variance have a constant term and a term
  proportional to the initial object size. This is inspired by the central
  limit theorem, which states that the sum of IID random variables approaches
  a normal distribution with mean and variance proportional to the number of
  variables in the sum. The theorem doesn't perfectly hold in this case, since
  there are some dependencies between AST nodes, but it does seem to be a good
  approximation.)

  Args:
    example_builder: Function that, when called, returns a random example whose
      size is (roughly) proportional to the function argument.
    desired_sizes: Padding that specifies the max sizes for each dimension. We
      will attempt to get close to this without exceeding it.
    success_probability: Proportion of generated examples we want to be able to
      keep, i.e. the proportion that should be smaller than the padding size.
    samples: How many random examples to generate.
    min_object_size: Minimum size of AST to generate while fitting.
    max_object_size: Maximum size of AST to generate while fitting.
    optimization_max_steps: Max iterations to fit size model.
    log_likelihood_epsilon: Stop optimizing when the loss changes by less than
      this amount in each iteration.
    round_to_powers_of_two: Whether to automatically round up the returned
      padding sizes to powers of two.
    progress: Wrapper around an iterable to use as a progress bar, to show
      progress during training (such as tqdm)

  Returns:
    - Calibrated target number of nodes to use as a generation target.
    - Padding configuration to use.
  """
  if progress is None:
    progress = lambda x: x

  # Collect samples.
  print("Generating data...")
  object_sizes = np.empty([samples], dtype="int")
  data = {
      "graph_nodes": np.empty([samples], dtype="int"),
      "graph_in_tagged_nodes": np.empty([samples], dtype="int"),
      "initial_transitions": np.empty([samples], dtype="int"),
      "in_tagged_transitions": np.empty([samples], dtype="int"),
      "edges": np.empty([samples], dtype="int"),
  }
  for i in progress(range(samples)):
    target_size = np.random.randint(min_object_size, max_object_size)
    example = example_builder(target_size)

    object_sizes[i] = target_size
    data["graph_nodes"][i] = example.graph_metadata.num_nodes
    data["graph_in_tagged_nodes"][i] = (
        example.graph_metadata.num_input_tagged_nodes)
    data["initial_transitions"][i] = (
        example.automaton_graph.initial_to_in_tagged.values.shape[0])
    data["in_tagged_transitions"][i] = (
        example.automaton_graph.in_tagged_to_in_tagged.values.shape[0])
    data["edges"][i] = example.edges.values.shape[0]

  # Fit models.
  print("Fitting a size model...")

  def single_log_likelihood(params, n, x):
    """Log likelihood of a single point under the size model."""
    base_mu, base_std, prop_mu, prop_std = params
    mu = base_mu + n * prop_mu
    var = base_std**2 + n * prop_std**2 + 1e-3
    return -0.5 * (jnp.log(var + 2 * jnp.pi) + (x - mu)**2 / var)

  def compute_loss(params, ns, xs):
    return -jnp.sum(
        jax.vmap(single_log_likelihood, in_axes=(None, 0, 0))(params, ns, xs))

  compute_loss_and_grads = jax.jit(jax.value_and_grad(compute_loss))
  opt_init, opt_update = optax.adam(0.1)

  model_params = {}
  for size_key, values in progress(data.items()):
    params = jnp.array([0., 1., 0., 1.])
    opt_state = opt_init(params)
    last_loss = None
    for i in progress(range(optimization_max_steps)):
      loss, grads = compute_loss_and_grads(params, object_sizes, values)
      if last_loss is not None and np.abs(last_loss -
                                          loss) < log_likelihood_epsilon:
        break
      last_loss = loss
      updates, opt_state = opt_update(grads, opt_state)
      params = optax.apply_updates(params, updates)
    print(f"Fit model for {size_key} after {i + 1} iterations, loss was {loss}")
    model_params[size_key] = params

  # Figure out which of the desired sizes is the most constraining.
  print("Solving for padding sizes...")
  desired_sizes = {
      "graph_nodes":
          desired_sizes.static_max_metadata.num_nodes,
      "graph_in_tagged_nodes":
          desired_sizes.static_max_metadata.num_input_tagged_nodes,
      "initial_transitions":
          desired_sizes.max_initial_transitions,
      "in_tagged_transitions":
          desired_sizes.max_in_tagged_transitions,
      "edges":
          desired_sizes.max_edges,
  }
  ast_constraints = []
  p = success_probability
  for size_key, target in desired_sizes.items():
    # Solve for the n such that the `p`th quantile of the distribution is the
    # target value; this ends up being a quadratic equation.
    base_mu, base_std, prop_mu, prop_std = model_params[size_key]
    erfval = scipy.special.erfinv(2 * p - 1)**2
    a = prop_mu**2
    b = (2 * prop_mu * (base_mu - target) - 2 * prop_std**2 * erfval)
    c = (target - base_mu)**2 - 2 * (base_std**2 + 1e-3) * erfval
    constraint = (-b - np.sqrt(b**2 - 4 * a * c)) / (2 * a)
    ast_constraints.append(constraint)
    print(size_key, "with slope", prop_mu, "constrains to", constraint)

  ast_target_count = int(min(ast_constraints))
  print(f"Target size {ast_target_count} satisfies all constraints with "
        "high probability")

  # Compute the `p`th quantile of each size, maybe rounding up.
  quantiles = {}
  for size_key, params in model_params.items():
    base_mu, base_std, prop_mu, prop_std = params
    mu = base_mu + ast_target_count * prop_mu
    var = base_std**2 + ast_target_count * prop_std**2 + 1e-3
    quantile = mu + np.sqrt(2 * var) * scipy.special.erfinv(2 * p - 1)
    if round_to_powers_of_two:
      # jump down by 0.5 to avoid off-by-one errors with the maximum value
      rounded_quantile = int(np.exp2(np.ceil(np.log2(quantile - 0.5))))
    else:
      rounded_quantile = int(np.ceil(quantile))

    print(f"{size_key}: Rounded {quantile} to {rounded_quantile}")
    quantiles[size_key] = rounded_quantile

  return ast_target_count, graph_bundle.PaddingConfig(
      static_max_metadata=automaton_builder.EncodedGraphMetadata(
          num_nodes=quantiles["graph_nodes"],
          num_input_tagged_nodes=quantiles["graph_in_tagged_nodes"]),
      max_initial_transitions=quantiles["initial_transitions"],
      max_in_tagged_transitions=quantiles["in_tagged_transitions"],
      max_edges=quantiles["edges"],
  )