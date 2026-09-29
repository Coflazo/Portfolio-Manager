#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <algorithm>
#include <cmath>
#include <numeric>
#include <stdexcept>
#include <vector>

namespace py = pybind11;
using Matrix = std::vector<std::vector<double>>;

py::dict simulate(const Matrix& opens, const Matrix& closes, const Matrix& fx_open,
                  const Matrix& fx_close, const Matrix& splits, const Matrix& dividends,
                  const Matrix& targets, double capital, double fee_bps, double slip_bps) {
    if (opens.empty() || opens.front().empty() || !std::isfinite(capital) || capital <= 0 ||
        !std::isfinite(fee_bps) || !std::isfinite(slip_bps) || fee_bps < 0 || slip_bps < 0 ||
        fee_bps >= 10000 || slip_bps >= 10000) throw std::invalid_argument("Invalid inputs");
    const auto n = opens.size(), m = opens.front().size();
    for (const auto* matrix : {&opens, &closes, &fx_open, &fx_close, &splits, &dividends, &targets}) {
        if (matrix->size() != n) throw std::invalid_argument("Mismatched rows");
        for (const auto& row : *matrix) {
            if (row.size() != m) throw std::invalid_argument("Mismatched columns");
            for (const auto value : row) {
                if (!std::isfinite(value) || value < 0 ||
                    (matrix != &dividends && matrix != &targets && value == 0))
                    throw std::invalid_argument("Invalid matrix value");
            }
        }
    }
    for (const auto& row : targets)
        if (std::accumulate(row.begin(), row.end(), 0.0) > 1.0 + 1e-12)
            throw std::invalid_argument("Leverage not supported");
    double cash = capital, fees = 0;
    const double rate = fee_bps / 10000, slip = slip_bps / 10000;
    std::vector<double> qty(m, 0), curve;
    int trades = 0;
    for (std::size_t t = 0; t < n; ++t) {
        for (std::size_t j = 0; j < m; ++j) {
            qty[j] *= splits[t][j];
            cash += qty[j] * dividends[t][j] * fx_open[t][j];
        }
        if (t > 0) {
            double nav = cash;
            for (std::size_t j = 0; j < m; ++j) nav += qty[j]*opens[t][j]*fx_open[t][j];
            std::vector<double> desired(m), needed(m), costs(m);
            for (std::size_t j = 0; j < m; ++j) {
                desired[j] = targets[t-1][j]*nav/(opens[t][j]*fx_open[t][j]);
                const double sell = qty[j] - desired[j];
                if (sell > 1e-10) {
                    const double value = sell*opens[t][j]*fx_open[t][j]*(1-slip);
                    const double fee = value*rate;
                    cash += value-fee;
                    qty[j] -= sell;
                    fees += fee;
                    ++trades;
                }
            }
            for (std::size_t j = 0; j < m; ++j) {
                needed[j] = std::max(0.0, desired[j]-qty[j]);
                costs[j] = needed[j]*opens[t][j]*fx_open[t][j]*(1+slip)*(1+rate);
            }
            const double total = std::accumulate(costs.begin(), costs.end(), 0.0);
            const double scale = total > 0 ? std::min(1.0, std::max(0.0, cash)/total) : 0;
            for (std::size_t j = 0; j < m; ++j) {
                const double buy = needed[j]*scale;
                if (buy > 1e-10) {
                    const double value = buy*opens[t][j]*fx_open[t][j]*(1+slip);
                    const double fee = value*rate;
                    cash -= value+fee;
                    qty[j] += buy;
                    fees += fee;
                    ++trades;
                }
            }
        }
        double nav = cash;
        for (std::size_t j = 0; j < m; ++j) nav += qty[j]*closes[t][j]*fx_close[t][j];
        curve.push_back(nav);
    }
    py::dict result;
    result["equity"] = curve; result["cash"] = cash; result["quantities"] = qty;
    result["trades"] = trades; result["fees"] = fees;
    return result;
}

PYBIND11_MODULE(_backtest, module) {
    module.doc() = "Daily multi-asset spot backtest, next-open execution, explicit FX and corporate actions";
    module.def("simulate", &simulate);
}
