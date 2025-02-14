// Copyright 2023 Xanadu Quantum Technologies Inc.

// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at

//     http://www.apache.org/licenses/LICENSE-2.0

// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#ifndef CATALYST_PASSES
#define CATALYST_PASSES

include "mlir/Pass/PassBase.td"

def CatalystBufferizationPass : Pass<"catalyst-bufferize"> {
    let summary = "Bufferize tensors in catalyst utility ops.";

    let dependentDialects = [
        "bufferization::BufferizationDialect",
        "memref::MemRefDialect",
        "index::IndexDialect"
    ];

    let constructor = "catalyst::createCatalystBufferizationPass()";
}

def ArrayListToMemRefPass : Pass<"convert-arraylist-to-memref"> {
    let summary = "Lower array list operations to memref operations.";
    let description = [{
        This pass implements dynamically resizable array lists via lowering
        them to mutable memrefs.
    }];

    let dependentDialects = [
        "mlir::arith::ArithDialect",
        "mlir::func::FuncDialect",
        "mlir::memref::MemRefDialect",
        "mlir::scf::SCFDialect"
    ];

    let constructor = "catalyst::createArrayListToMemRefPass()";
}

def CatalystConversionPass : Pass<"convert-catalyst-to-llvm"> {
    let summary = "Lower catalyst utility operations to the LLVM dialect.";

    let dependentDialects = [
        "mlir::LLVM::LLVMDialect",
    ];

    let constructor = "catalyst::createCatalystConversionPass()";
}

def ScatterLoweringPass : Pass<"scatter-lowering"> {
    let summary = "Lower scatter op from Stable HLO to loops.";

    let dependentDialects = [
        "index::IndexDialect",
        "mhlo::MhloDialect",
        "scf::SCFDialect"
    ];

    let constructor = "catalyst::createScatterLoweringPass()";
}

def HloCustomCallLoweringPass : Pass<"hlo-custom-call-lowering"> {
    let summary = "Lower custom calls op from Stable HLO to CallOp.";

    let dependentDialects = [
        "index::IndexDialect",
        "mlir::func::FuncDialect",
        "catalyst::CatalystDialect",
    ];

    let constructor = "catalyst::createHloCustomCallLoweringPass()";
}

def QnodeToAsyncLoweringPass : Pass<"qnode-to-async-lowering"> {
    let summary = "Lower Qnode func and call operations to async func and call operations.";

    let dependentDialects = [
        "async::AsyncDialect",
        "mlir::memref::MemRefDialect",
        "bufferization::BufferizationDialect"
    ];

    let constructor = "catalyst::QnodeToAsyncLoweringPass()";
}
#endif // CATALYST_PASSES
