"""GUI smoke test (offscreen): build the window, connect in sim mode,
acquire a point programmatically, and write a .vol."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "devices"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from balcal_gui.app.main_window import BalanceCalWindow
from balcal_gui.session import TestPoint


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def win(qapp):
    w = BalanceCalWindow(sim=True)
    yield w
    w._disconnect()
    w.close()


def test_window_builds(win):
    assert win.tabs.count() == 4
    assert win.orient_combo.count() == 12
    assert win.chan_table.rowCount() == 7          # preview before connect


def test_connect_and_live_sim(win, qapp):
    win.sim_check.setChecked(True)
    win._connect()
    assert win.daq is not None and win.daq.connected
    assert win.lamp.text() == "SIMULATION"
    win._refresh_live()
    assert "N1" in win.live_label.text()


def test_history_channel_toggles(win, qapp):
    """Time-history plot channel visibility: one colored checkbox per
    plotted channel; unchecking hides the curve; the choice persists
    across re-acquisitions."""
    win.sim_check.setChecked(True)
    win._connect()
    acq = win.daq.acquire(0.2, win.session.kind)
    win._plot_acquisition(acq)
    assert win._hist_checks                        # checkboxes created
    name = next(iter(win._hist_curves))
    assert win._hist_curves[name].isVisible()
    win._hist_checks[name].setChecked(False)
    assert not win._hist_curves[name].isVisible()
    # persists across a new acquisition
    win._plot_acquisition(acq)
    assert not win._hist_curves[name].isVisible()
    assert "Excitation" not in win._hist_curves    # never plotted
    win._hist_checks[name].setChecked(True)
    assert win._hist_curves[name].isVisible()


def test_acquire_and_write(win, qapp, tmp_path):
    win.sim_check.setChecked(True)
    win._connect()
    win.seconds_spin.setValue(0.2)
    acq = win.daq.acquire(0.2, win.session.kind)
    win._pending_load = 10.0
    win._acquire_done(acq)
    assert win.session.point_count() == 1
    assert win.mtable.rowCount() == 1

    win.operator_edit.setText("smoke")
    win._sync_session_meta()
    out = tmp_path / "smoke.vol"
    from balcal_gui.volfile import write_vol
    write_vol(win.session, str(out))
    text = out.read_text()
    assert text.startswith("Voltage Calibration File 3.1")
    assert "[N1 pos]" in text


def test_orientation_switch_updates_ui(win):
    win.orient_combo.setCurrentText("Mx_pos")
    assert "Mx_pos" in win.acquire_btn.text()
    assert win.arm_spin.value() == 2.0             # default roll arm


def test_point_filed_under_captured_orientation(win, qapp):
    """The point belongs to the orientation captured at acquire time,
    even if the combo were changed before the worker finished."""
    win.sim_check.setChecked(True)
    win._connect()
    acq = win.daq.acquire(0.2, win.session.kind)
    win.orient_combo.setCurrentText("N1_pos")
    win._pending_key = "N1_pos"
    win._pending_load = 10.0
    win.orient_combo.setCurrentText("N2_pos")      # operator flips combo
    win._acquire_done(acq)
    assert "N1_pos" in win.session.points
    assert "N2_pos" not in win.session.points


def test_load_is_weight_times_arm(win):
    win.orient_combo.setCurrentText("N1_pos")
    win.arm_spin.setValue(1.0)
    assert win._compute_load(25.0) == 25.0
    win.arm_spin.setValue(3.5)                     # operator override
    assert win._compute_load(10.0) == 35.0


def test_balance_type_switch_clears_info_cells(win, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    win.max_table.item(0, 1).setText("100")
    win.max_table.item(0, 2).setText("1.5")
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    win.balance_type_combo.setCurrentText("Moment Balance")
    win._sync_session_meta()
    assert win.session.max_loads == {}
    assert win.session.distances == {}
    assert win.max_table.item(0, 0).text() == "Aft_Pitch"
    assert win.max_table.item(0, 1).text() == ""


def test_balance_type_switch_confirms_with_points(win, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    win.session.add_point("N1_pos", TestPoint(load=1.0, volts=[0.0] * 6,
                                              excitation=10.0))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.No)
    win.balance_type_combo.setCurrentText("Moment Balance")
    # declined → combo reverted, points kept
    assert win.balance_type_combo.currentText() == "Force Balance"
    assert win.session.point_count() == 1


def test_load_vol_populates_ui_and_appends(win, qapp, tmp_path,
                                           monkeypatch):
    """Reload a written .vol, verify the UI is repopulated, append a
    point, rewrite, and confirm the union survives."""
    from balcal_gui.volfile import read_vol_session, write_vol

    win.operator_edit.setText("first pass")
    win.serial_edit.setText("SN-42")
    win.max_table.item(0, 1).setText("100")
    win.orient_combo.setCurrentText("N1_pos")
    win.session.add_point("N1_pos", TestPoint(
        load=10.0, volts=[1e-4] * 6, excitation=10.0))
    win._sync_session_meta()
    path = tmp_path / "editable.vol"
    write_vol(win.session, str(path))

    # fresh window state, then load the file back
    win.session.points.clear()
    win.operator_edit.setText("")
    monkeypatch.setattr(
        "PyQt6.QtWidgets.QFileDialog.getOpenFileName",
        staticmethod(lambda *a, **k: (str(path), "")))
    win._load_vol()
    assert win.operator_edit.text() == "first pass"
    assert win.serial_edit.text() == "SN-42"
    assert win.session.point_count() == 1
    assert win.orient_combo.currentText() == "N1_pos"
    assert win.mtable.rowCount() == 1

    # append and rewrite
    win.session.add_point("N1_pos", TestPoint(
        load=20.0, volts=[2e-4] * 6, excitation=10.0))
    win._sync_session_meta()
    out = tmp_path / "appended.vol"
    write_vol(win.session, str(out))
    r = read_vol_session(str(out))
    assert [p.load for p in r.points["N1_pos"]] == [10.0, 20.0]


def test_acquired_point_stores_stds(win, qapp):
    win.sim_check.setChecked(True)
    win._connect()
    acq = win.daq.acquire(0.2, win.session.kind)
    win._pending_key = "N1_pos"
    win._pending_load = 5.0
    win._acquire_done(acq)
    p = win.session.points["N1_pos"][0]
    assert p.stds is not None and len(p.stds) == 6
    assert all(v >= 0 for v in p.stds)


def test_cal_plot_updates_with_points(win):
    win.orient_combo.setCurrentText("N1_pos")
    for load, std in ((0.0, 1e-5), (10.0, 1e-5), (20.0, 9e-4)):
        win.session.add_point("N1_pos", TestPoint(
            load=load, volts=[load * 1e-4] * 6, excitation=10.0,
            stds=[std] * 6))
    win._refresh_mtable()
    # scatter + error bars + trend line present
    assert len(win.cal_plot.plotItem.items) >= 2
    assert "3 points" in win.cal_plot.plotItem.titleLabel.text


def test_device_panel_requires_connection(win, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    seen = {}
    monkeypatch.setattr(QMessageBox, "information",
                        lambda *a, **k: seen.setdefault("warned", True))
    win._open_device_panel()
    assert seen.get("warned")


def test_device_panel_opens_shared(win, qapp):
    win.sim_check.setChecked(True)
    win._connect()
    win._open_device_panel()
    panel_win = getattr(win, "_panel_win", None)
    assert panel_win is not None and panel_win.isVisible()
    # the panel must share the SAME driver instance, not a second one
    assert panel_win.centralWidget().device is win.daq.driver
    panel_win.close()


def _fill_linear_session(win, bad_point=False):
    """Populate a fittable session; optionally corrupt one point."""
    sens = 1.34e-4
    win.max_table.item(0, 1).setText("100")
    for i, el in enumerate(win.session.elements):
        for positive in (True, False):
            key = f"{el.name}_{'pos' if positive else 'neg'}"
            sign = 1 if positive else -1
            for load in (0, 10, 20, 30, 20, 10, 0):
                volts = [1e-4] * 6
                volts[i] += sens * sign * load * 9.86
                win.session.add_point(key, TestPoint(
                    load=sign * load, volts=volts, excitation=9.86))
    if bad_point:
        p = win.session.points["N1_pos"][3]
        p.volts = list(p.volts)
        p.volts[0] += 0.005


def test_compute_summary_with_diagnostics(win, qapp):
    _fill_linear_session(win, bad_point=True)
    win._compute_summary()
    text = win.summary_text.toPlainText()
    assert "Fit diagnostics" in text
    assert "outlier" in text.lower()
    assert win.diag_element_combo.count() == 6
    # auto-jumped to the element with the outlier (N1 = index 0)
    assert win.diag_element_combo.currentIndex() == 0
    assert len(win._diag.outliers()) >= 1
    assert len(win.diag_plot.plotItem.items) >= 3   # ref + pts + outliers


def test_exclude_outlier_recovers_r2(win, qapp):
    _fill_linear_session(win, bad_point=True)
    win._compute_summary()
    r2_before = win._diag.r_squared[0]
    out = win._diag.outliers()[0]
    win._diag_sel = out
    win._diag_exclude()                    # excludes + auto-recomputes
    assert win.session.points[out.key][out.index].excluded
    assert win._diag.r_squared[0] > r2_before
    assert win._diag.r_squared[0] > 0.9999
    # measurement table shows it grayed with the marker
    win.orient_combo.setCurrentText(out.key)
    assert "(excl)" in win.mtable.item(out.index, 0).text()
    # and re-include restores it
    win._diag_include_all()
    assert win.session.excluded_count() == 0


def test_diag_delete_removes_point(win, qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    _fill_linear_session(win, bad_point=True)
    win._compute_summary()
    out = win._diag.outliers()[0]
    n0 = win.session.point_count()
    win._diag_sel = out
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    win._diag_delete()
    assert win.session.point_count() == n0 - 1
    assert not win._diag.outliers()        # refit is clean


def test_diag_goto_selects_row(win, qapp):
    _fill_linear_session(win, bad_point=True)
    win._compute_summary()
    out = win._diag.outliers()[0]
    win._diag_sel = out
    win._diag_goto()
    assert win.tabs.currentIndex() == 1
    assert win.orient_combo.currentText() == out.key
    assert win.mtable.currentRow() == out.index


def test_multi_row_delete_maps_indices(win, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    win.orient_combo.setCurrentText("N1_pos")
    for load in (1.0, 2.0, 3.0):
        win.session.add_point("N1_pos",
                              TestPoint(load=load, volts=[0.0] * 6,
                                        excitation=10.0))
    win._refresh_mtable()
    win.mtable.selectRow(0)
    sel = win.mtable.selectionModel()
    from PyQt6.QtCore import QItemSelectionModel
    sel.select(win.mtable.model().index(1, 0),
               QItemSelectionModel.SelectionFlag.Select
               | QItemSelectionModel.SelectionFlag.Rows)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    win._delete_row()
    remaining = win.session.points["N1_pos"]
    assert [p.load for p in remaining] == [3.0]
