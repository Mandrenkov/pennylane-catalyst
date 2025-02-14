// Copyright 2022-2023 Xanadu Quantum Technologies Inc.

// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at

//     http://www.apache.org/licenses/LICENSE-2.0

// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include "LightningKokkosSimulator.hpp"

namespace Catalyst::Runtime::Simulator {

auto LightningKokkosSimulator::AllocateQubit() -> QubitIdType
{
    const size_t num_qubits = this->device_sv->getNumQubits();
    this->device_sv = std::make_unique<StateVectorT>(num_qubits + 1);
    return this->qubit_manager.Allocate(num_qubits);
}

auto LightningKokkosSimulator::AllocateQubits(size_t num_qubits) -> std::vector<QubitIdType>
{
    if (!num_qubits) {
        return {};
    }

    const size_t cur_num_qubits = this->device_sv->getNumQubits();
    const size_t new_num_qubits = cur_num_qubits + num_qubits;
    this->device_sv = std::make_unique<StateVectorT>(new_num_qubits);
    return this->qubit_manager.AllocateRange(cur_num_qubits, new_num_qubits);
}

void LightningKokkosSimulator::ReleaseQubit(QubitIdType q) { this->qubit_manager.Release(q); }

void LightningKokkosSimulator::ReleaseAllQubits()
{
    this->qubit_manager.ReleaseAll();
    this->device_sv = std::make_unique<StateVectorT>(0); // reset the device
}

auto LightningKokkosSimulator::GetNumQubits() const -> size_t
{
    return this->device_sv->getNumQubits();
}

void LightningKokkosSimulator::StartTapeRecording()
{
    RT_FAIL_IF(this->tape_recording, "Cannot re-activate the cache manager");
    this->tape_recording = true;
    this->cache_manager.Reset();
}

void LightningKokkosSimulator::StopTapeRecording()
{
    RT_FAIL_IF(!this->tape_recording, "Cannot stop an already stopped cache manager");
    this->tape_recording = false;
}

auto LightningKokkosSimulator::CacheManagerInfo()
    -> std::tuple<size_t, size_t, size_t, std::vector<std::string>, std::vector<ObsIdType>>
{
    return {this->cache_manager.getNumOperations(), this->cache_manager.getNumObservables(),
            this->cache_manager.getNumParams(), this->cache_manager.getOperationsNames(),
            this->cache_manager.getObservablesKeys()};
}

void LightningKokkosSimulator::SetDeviceShots(size_t shots) { this->device_shots = shots; }

auto LightningKokkosSimulator::GetDeviceShots() const -> size_t { return this->device_shots; }

void LightningKokkosSimulator::PrintState()
{
    using std::cout;
    using std::endl;
    using UnmanagedComplexHostView = Kokkos::View<Kokkos::complex<double> *, Kokkos::HostSpace,
                                                  Kokkos::MemoryTraits<Kokkos::Unmanaged>>;

    const size_t num_qubits = this->device_sv->getNumQubits();
    const size_t size = Pennylane::Util::exp2(num_qubits);

    std::vector<std::complex<double>> state(size, {0.0, 0.0});
    auto *state_kptr = reinterpret_cast<Kokkos::complex<double> *>(state.data());
    auto device_data = this->device_sv->getView();
    Kokkos::deep_copy(UnmanagedComplexHostView(state_kptr, size), device_data);

    size_t idx = 0;
    cout << "*** State-Vector of Size " << size << " ***" << endl;
    cout << "[";
    for (; idx < size - 1; idx++) {
        cout << state[idx] << ", ";
    }
    cout << state[idx] << "]" << endl;
}

auto LightningKokkosSimulator::Zero() const -> Result
{
    return const_cast<Result>(&GLOBAL_RESULT_FALSE_CONST);
}

auto LightningKokkosSimulator::One() const -> Result
{
    return const_cast<Result>(&GLOBAL_RESULT_TRUE_CONST);
}

void LightningKokkosSimulator::NamedOperation(const std::string &name,
                                              const std::vector<double> &params,
                                              const std::vector<QubitIdType> &wires, bool inverse)
{
    // First, check if operation `name` is supported by the simulator
    auto &&[op_num_wires, op_num_params] =
        Lightning::lookup_gates(Lightning::simulator_gate_info, name);

    // Check the validity of number of qubits and parameters
    RT_FAIL_IF((!wires.size() && wires.size() != op_num_wires), "Invalid number of qubits");
    RT_FAIL_IF(params.size() != op_num_params, "Invalid number of parameters");

    // Convert wires to device wires
    auto &&dev_wires = getDeviceWires(wires);

    // Update the state-vector
    this->device_sv->applyOperation(name, dev_wires, inverse, params);

    // Update tape caching if required
    if (this->tape_recording) {
        this->cache_manager.addOperation(name, params, dev_wires, inverse);
    }
}

void LightningKokkosSimulator::MatrixOperation(const std::vector<std::complex<double>> &matrix,
                                               const std::vector<QubitIdType> &wires, bool inverse)
{
    using UnmanagedComplexHostView = Kokkos::View<Kokkos::complex<double> *, Kokkos::HostSpace,
                                                  Kokkos::MemoryTraits<Kokkos::Unmanaged>>;

    // Check the validity of number of qubits and parameters
    RT_FAIL_IF(!wires.size(), "Invalid number of qubits");

    // Convert wires to device wires
    auto &&dev_wires = getDeviceWires(wires);

    std::vector<Kokkos::complex<double>> matrix_kok;
    matrix_kok.resize(matrix.size());
    std::transform(matrix.begin(), matrix.end(), matrix_kok.begin(),
                   [](auto c) { return static_cast<Kokkos::complex<double>>(c); });

    Kokkos::View<Kokkos::complex<double> *> gate_matrix("gate_matrix", matrix_kok.size());
    Kokkos::deep_copy(gate_matrix, UnmanagedComplexHostView(matrix_kok.data(), matrix_kok.size()));

    // Update the state-vector
    this->device_sv->applyMultiQubitOp(gate_matrix, dev_wires, inverse);

    // Update tape caching if required
    if (this->tape_recording) {
        this->cache_manager.addOperation("MatrixOp", {}, dev_wires, inverse);
    }
}

auto LightningKokkosSimulator::Observable(ObsId id, const std::vector<std::complex<double>> &matrix,
                                          const std::vector<QubitIdType> &wires) -> ObsIdType
{
    RT_FAIL_IF(wires.size() > this->GetNumQubits(), "Invalid number of wires");
    RT_FAIL_IF(!isValidQubits(wires), "Invalid given wires");

    auto &&dev_wires = getDeviceWires(wires);

    if (id == ObsId::Hermitian) {
        return this->obs_manager.createHermitianObs(matrix, dev_wires);
    }

    return this->obs_manager.createNamedObs(id, dev_wires);
}

auto LightningKokkosSimulator::TensorObservable(const std::vector<ObsIdType> &obs) -> ObsIdType
{
    return this->obs_manager.createTensorProdObs(obs);
}

auto LightningKokkosSimulator::HamiltonianObservable(const std::vector<double> &coeffs,
                                                     const std::vector<ObsIdType> &obs) -> ObsIdType
{
    return this->obs_manager.createHamiltonianObs(coeffs, obs);
}

auto LightningKokkosSimulator::Expval(ObsIdType obsKey) -> double
{
    RT_FAIL_IF(!this->obs_manager.isValidObservables({obsKey}),
               "Invalid key for cached observables");

    // update tape caching
    if (this->tape_recording) {
        cache_manager.addObservable(obsKey, MeasurementsT::Expval);
    }

    auto &&obs = this->obs_manager.getObservable(obsKey);

    Pennylane::LightningKokkos::Measures::Measurements<StateVectorT> m{*(this->device_sv)};

    return device_shots ? m.expval(*obs, device_shots, {}) : m.expval(*obs);
}

auto LightningKokkosSimulator::Var(ObsIdType obsKey) -> double
{
    RT_FAIL_IF(!this->obs_manager.isValidObservables({obsKey}),
               "Invalid key for cached observables");

    // update tape caching
    if (this->tape_recording) {
        this->cache_manager.addObservable(obsKey, MeasurementsT::Var);
    }

    auto &&obs = this->obs_manager.getObservable(obsKey);

    Pennylane::LightningKokkos::Measures::Measurements<StateVectorT> m{*(this->device_sv)};

    return device_shots ? m.var(*obs, device_shots) : m.var(*obs);
}

void LightningKokkosSimulator::State(DataView<std::complex<double>, 1> &state)
{
    using UnmanagedComplexHostView = Kokkos::View<Kokkos::complex<double> *, Kokkos::HostSpace,
                                                  Kokkos::MemoryTraits<Kokkos::Unmanaged>>;

    const size_t num_qubits = this->device_sv->getNumQubits();
    const size_t size = Pennylane::Util::exp2(num_qubits);
    RT_FAIL_IF(state.size() != size, "Invalid size for the pre-allocated state vector");

    // create a temporary buffer to copy the underlying state-vector to
    std::vector<std::complex<double>> buffer(size);
    auto *state_kptr = reinterpret_cast<Kokkos::complex<double> *>(buffer.data());

    // copy data from device to host
    auto device_data = this->device_sv->getView();
    Kokkos::deep_copy(UnmanagedComplexHostView(state_kptr, size), device_data);

    // move data to state leveraging MemRefIter
    std::move(buffer.begin(), buffer.end(), state.begin());
}

void LightningKokkosSimulator::Probs(DataView<double, 1> &probs)
{
    Pennylane::LightningKokkos::Measures::Measurements<StateVectorT> m{*(this->device_sv)};
    auto &&dv_probs = device_shots ? m.probs(device_shots) : m.probs();

    RT_FAIL_IF(probs.size() != dv_probs.size(), "Invalid size for the pre-allocated probabilities");

    std::move(dv_probs.begin(), dv_probs.end(), probs.begin());
}

void LightningKokkosSimulator::PartialProbs(DataView<double, 1> &probs,
                                            const std::vector<QubitIdType> &wires)
{
    const size_t numWires = wires.size();
    const size_t numQubits = this->GetNumQubits();

    RT_FAIL_IF(numWires > numQubits, "Invalid number of wires");
    RT_FAIL_IF(!isValidQubits(wires), "Invalid given wires to measure");

    auto dev_wires = getDeviceWires(wires);
    Pennylane::LightningKokkos::Measures::Measurements<StateVectorT> m{*(this->device_sv)};
    auto &&dv_probs = device_shots ? m.probs(dev_wires, device_shots) : m.probs(dev_wires);

    RT_FAIL_IF(probs.size() != dv_probs.size(),
               "Invalid size for the pre-allocated partial-probabilities");

    std::move(dv_probs.begin(), dv_probs.end(), probs.begin());
}

void LightningKokkosSimulator::Sample(DataView<double, 2> &samples, size_t shots)
{
    Pennylane::LightningKokkos::Measures::Measurements<StateVectorT> m{*(this->device_sv)};
    // PL-Lightning-Kokkos generates samples using the alias method.
    // Reference: https://en.wikipedia.org/wiki/Inverse_transform_sampling
    auto li_samples = m.generate_samples(shots);

    RT_FAIL_IF(samples.size() != li_samples.size(), "Invalid size for the pre-allocated samples");

    const size_t numQubits = this->GetNumQubits();

    // The lightning samples are layed out as a single vector of size
    // shots*qubits, where each element represents a single bit. The
    // corresponding shape is (shots, qubits). Gather the desired bits
    // corresponding to the input wires into a bitstring.
    auto samplesIter = samples.begin();
    for (size_t shot = 0; shot < shots; shot++) {
        for (size_t wire = 0; wire < numQubits; wire++) {
            *(samplesIter++) = static_cast<double>(li_samples[shot * numQubits + wire]);
        }
    }
}
void LightningKokkosSimulator::PartialSample(DataView<double, 2> &samples,
                                             const std::vector<QubitIdType> &wires, size_t shots)
{
    const size_t numWires = wires.size();
    const size_t numQubits = this->GetNumQubits();

    RT_FAIL_IF(numWires > numQubits, "Invalid number of wires");
    RT_FAIL_IF(!isValidQubits(wires), "Invalid given wires to measure");
    RT_FAIL_IF(samples.size() != shots * numWires,
               "Invalid size for the pre-allocated partial-samples");

    // get device wires
    auto &&dev_wires = getDeviceWires(wires);

    // generate_samples is a member function of the MeasuresKokkos class.
    Pennylane::LightningKokkos::Measures::Measurements<StateVectorT> m{*(this->device_sv)};

    // PL-Lightning-Kokkos generates samples using the alias method.
    // Reference: https://en.wikipedia.org/wiki/Inverse_transform_sampling
    auto li_samples = m.generate_samples(shots);

    // The lightning samples are layed out as a single vector of size
    // shots*qubits, where each element represents a single bit. The
    // corresponding shape is (shots, qubits). Gather the desired bits
    // corresponding to the input wires into a bitstring.
    auto samplesIter = samples.begin();
    for (size_t shot = 0; shot < shots; shot++) {
        for (auto wire : dev_wires) {
            *(samplesIter++) = static_cast<double>(li_samples[shot * numQubits + wire]);
        }
    }
}

void LightningKokkosSimulator::Counts(DataView<double, 1> &eigvals, DataView<int64_t, 1> &counts,
                                      size_t shots)
{
    const size_t numQubits = this->GetNumQubits();
    const size_t numElements = 1U << numQubits;

    RT_FAIL_IF(eigvals.size() != numElements || counts.size() != numElements,
               "Invalid size for the pre-allocated counts");

    // generate_samples is a member function of the MeasuresKokkos class.
    Pennylane::LightningKokkos::Measures::Measurements<StateVectorT> m{*(this->device_sv)};

    // PL-Lightning-Kokkos generates samples using the alias method.
    // Reference: https://en.wikipedia.org/wiki/Inverse_transform_sampling
    auto li_samples = m.generate_samples(shots);

    // Fill the eigenvalues with the integer representation of the corresponding
    // computational basis bitstring. In the future, eigenvalues can also be
    // obtained from an observable, hence the bitstring integer is stored as a
    // double.
    std::iota(eigvals.begin(), eigvals.end(), 0);
    std::fill(counts.begin(), counts.end(), 0);

    // The lightning samples are layed out as a single vector of size
    // shots*qubits, where each element represents a single bit. The
    // corresponding shape is (shots, qubits). Gather the bits of all qubits
    // into a bitstring.
    for (size_t shot = 0; shot < shots; shot++) {
        std::bitset<CHAR_BIT * sizeof(double)> basisState;
        size_t idx = 0;
        for (size_t wire = 0; wire < numQubits; wire++) {
            basisState[idx++] = li_samples[shot * numQubits + wire];
        }
        counts(static_cast<size_t>(basisState.to_ulong())) += 1;
    }
}

void LightningKokkosSimulator::PartialCounts(DataView<double, 1> &eigvals,
                                             DataView<int64_t, 1> &counts,
                                             const std::vector<QubitIdType> &wires, size_t shots)
{
    const size_t numWires = wires.size();
    const size_t numQubits = this->GetNumQubits();
    const size_t numElements = 1U << numWires;

    RT_FAIL_IF(numWires > numQubits, "Invalid number of wires");
    RT_FAIL_IF(!isValidQubits(wires), "Invalid given wires to measure");
    RT_FAIL_IF((eigvals.size() != numElements || counts.size() != numElements),
               "Invalid size for the pre-allocated partial-counts");

    // get device wires
    auto &&dev_wires = getDeviceWires(wires);

    // generate_samples is a member function of the MeasuresKokkos class.
    Pennylane::LightningKokkos::Measures::Measurements<StateVectorT> m{*(this->device_sv)};

    // PL-Lightning-Kokkos generates samples using the alias method.
    // Reference: https://en.wikipedia.org/wiki/Inverse_transform_sampling
    auto li_samples = m.generate_samples(shots);

    // Fill the eigenvalues with the integer representation of the corresponding
    // computational basis bitstring. In the future, eigenvalues can also be
    // obtained from an observable, hence the bitstring integer is stored as a
    // double.
    std::iota(eigvals.begin(), eigvals.end(), 0);
    std::fill(counts.begin(), counts.end(), 0);

    // The lightning samples are layed out as a single vector of size
    // shots*qubits, where each element represents a single bit. The
    // corresponding shape is (shots, qubits). Gather the desired bits
    // corresponding to the input wires into a bitstring.
    for (size_t shot = 0; shot < shots; shot++) {
        std::bitset<CHAR_BIT * sizeof(double)> basisState;
        size_t idx = 0;
        for (auto wire : dev_wires) {
            basisState[idx++] = li_samples[shot * numQubits + wire];
        }
        counts(static_cast<size_t>(basisState.to_ulong())) += 1;
    }
}

auto LightningKokkosSimulator::Measure(QubitIdType wire) -> Result
{
    using UnmanagedComplexHostView = Kokkos::View<Kokkos::complex<double> *, Kokkos::HostSpace,
                                                  Kokkos::MemoryTraits<Kokkos::Unmanaged>>;

    // get a measurement
    std::vector<QubitIdType> wires = {reinterpret_cast<QubitIdType>(wire)};

    std::vector<double> probs(1U << wires.size());
    DataView<double, 1> buffer_view(probs);
    this->PartialProbs(buffer_view, wires);

    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_real_distribution<> dis(0., 1.);
    float draw = dis(gen);
    bool mres = draw > probs[0];

    const size_t num_qubits = this->GetNumQubits();

    auto &&dev_wires = this->getDeviceWires(wires);
    const auto stride = pow(2, num_qubits - (1 + dev_wires[0]));
    const auto vec_size = pow(2, num_qubits);
    const auto section_size = vec_size / stride;
    const auto half_section_size = section_size / 2;

    std::vector<Kokkos::complex<double>> state(vec_size);

    // copy data from device to host
    auto device_data = this->device_sv->getView();
    Kokkos::deep_copy(UnmanagedComplexHostView(state.data(), vec_size), device_data);

    // zero half the entries
    // the "half" entries depend on the stride
    // *_*_*_*_ for stride 1
    // **__**__ for stride 2
    // ****____ for stride 4
    const size_t k = mres ? 0 : 1;
    for (size_t idx = 0; idx < half_section_size; idx++) {
        for (size_t ids = 0; ids < stride; ids++) {
            auto v = stride * (k + 2 * idx) + ids;
            state[v] = Kokkos::complex<double>(0.0, 0.0);
        }
    }

    // get the total of the new vector (since we need to normalize)
    double total =
        std::accumulate(state.begin(), state.end(), 0.0, [](double sum, Kokkos::complex<double> c) {
            return sum + real(c * conj(c));
        });

    // normalize the vector
    double norm = std::sqrt(total);
    std::for_each(state.begin(), state.end(), [norm](auto &elem) { elem /= norm; });

    this->device_sv = std::make_unique<StateVectorT>(state.data(), vec_size);

    return mres ? this->One() : this->Zero();
}

void LightningKokkosSimulator::Gradient(std::vector<DataView<double, 1>> &gradients,
                                        const std::vector<size_t> &trainParams)
{
    const bool tp_empty = trainParams.empty();
    const size_t num_observables = this->cache_manager.getNumObservables();
    const size_t num_params = this->cache_manager.getNumParams();
    const size_t num_train_params = tp_empty ? num_params : trainParams.size();
    const size_t jac_size = num_train_params * this->cache_manager.getNumObservables();

    if (!jac_size) {
        return;
    }

    RT_FAIL_IF(gradients.size() != num_observables, "Invalid number of pre-allocated gradients");

    auto &&obs_callees = this->cache_manager.getObservablesCallees();
    bool is_valid_measurements =
        std::all_of(obs_callees.begin(), obs_callees.end(),
                    [](const auto &m) { return m == MeasurementsT::Expval; });
    RT_FAIL_IF(!is_valid_measurements,
               "Unsupported measurements to compute gradient; "
               "Adjoint differentiation method only supports expectation return type");

    // Create OpsData
    auto &&ops_names = this->cache_manager.getOperationsNames();
    auto &&ops_params = this->cache_manager.getOperationsParameters();
    auto &&ops_wires = this->cache_manager.getOperationsWires();
    auto &&ops_inverses = this->cache_manager.getOperationsInverses();

    const auto &&ops = Pennylane::Algorithms::OpsData<StateVectorT>(ops_names, ops_params,
                                                                    ops_wires, ops_inverses);

    // Create the vector of observables
    auto &&obs_keys = this->cache_manager.getObservablesKeys();
    std::vector<std::shared_ptr<Pennylane::Observables::Observable<StateVectorT>>> obs_vec;
    obs_vec.reserve(obs_keys.size());
    for (auto idx : obs_keys) {
        obs_vec.emplace_back(this->obs_manager.getObservable(idx));
    }

    std::vector<size_t> all_params;
    if (tp_empty) {
        all_params.reserve(num_params);
        for (size_t i = 0; i < num_params; i++) {
            all_params.push_back(i);
        }
    }

    auto &&state = this->device_sv->getDataVector();

    // construct the Jacobian data
    Pennylane::Algorithms::JacobianData<StateVectorT> tape{
        num_params, state.size(), state.data(), obs_vec, ops, tp_empty ? all_params : trainParams};

    Pennylane::LightningKokkos::Algorithms::AdjointJacobian<StateVectorT> adj;
    std::vector<double> jacobian(jac_size, 0);
    adj.adjointJacobian(std::span{jacobian}, tape,
                        /* ref_data */ *this->device_sv,
                        /* apply_operations */ false);

    std::vector<double> cur_buffer(num_train_params);
    auto begin_loc_iter = jacobian.begin();
    for (size_t obs_idx = 0; obs_idx < num_observables; obs_idx++) {
        RT_ASSERT(begin_loc_iter != jacobian.end());
        RT_ASSERT(num_train_params <= gradients[obs_idx].size());
        std::move(begin_loc_iter, begin_loc_iter + num_train_params, cur_buffer.begin());
        std::move(cur_buffer.begin(), cur_buffer.end(), gradients[obs_idx].begin());
        begin_loc_iter += num_train_params;
    }
}

} // namespace Catalyst::Runtime::Simulator

GENERATE_DEVICE_FACTORY(LightningKokkosSimulator,
                        Catalyst::Runtime::Simulator::LightningKokkosSimulator);
